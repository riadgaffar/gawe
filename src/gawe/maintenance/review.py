"""The daily_review job kind: re-check yesterday's policy decisions and draw the keyed sample.

For each run with a `policy.decided` event that UTC day, in the job's tenant:

1. The run's chain must verify. Every stored step decision must rebuild to the payload of one of
   the run's `policy.decided` events; one that does not is `audit_mismatch`. A day's event that no
   stored decision matches is `superseded` when a recipient refresh replaced the decision, and
   `audit_mismatch` otherwise.
2. For decisions audited that day: the signature must verify (`signature_invalid`) and the stored
   arguments must still hash to the decided action (`action_mismatch`).
3. Automatic actions that executed are evaluated again under the current rules, with the run's
   findings and today's values. A different outcome is `rules_changed` when the rule-set digest
   differs, `inputs_changed` when it does not, and `rules_may_have_changed` for a decision from
   before digests existed.
4. Automatic executed actions the app marks as sample candidates are drawn with the review key
   (`sampled_low`).

Items are insert-once with deterministic ids and each gets one `review.flagged` or
`review.sampled` event on its run, so a repeated review (a crash, a second sweeper) adds nothing
new. A run that has ended gets its anchor refreshed past the new events. The review only reads
and records; it never changes a run, a step or a decision.
"""

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from gawe.core.anchor import Anchorer
from gawe.core.audit import AuditLog, AuditStore, verify_chain
from gawe.core.canonical import action_hash, canonical_json, sha256_hex
from gawe.core.maintenance import MaintenanceJob, MaintenanceStore
from gawe.core.models import PolicyDecision
from gawe.core.policy.audit_payload import decided_payload
from gawe.core.policy.engine import RuleEngine
from gawe.core.policy.rules import EvaluationContext
from gawe.core.policy.signing import PolicySigner
from gawe.core.records import StepNotFoundError, StepRecord, StepStore
from gawe.core.registry import ToolNotFoundError, ToolRegistry
from gawe.core.review import (
    CATCH_UP_DAYS,
    DEFAULT_RATE,
    KIND,
    Reason,
    ReviewItem,
    ReviewKey,
    ReviewStore,
    day_window,
    item_identity,
    review_job,
)
from gawe.core.runs import RunNotFoundError, RunRecord, RunStore
from gawe.core.statemachine import RUN_TERMINAL, StepStatus
from gawe.core.telemetry import NoopTelemetry, Telemetry
from gawe.core.verification import READBACK_KIND

ACTOR = "gawe.review"
EXECUTED = frozenset(
    {
        StepStatus.VERIFYING,
        StepStatus.AWAITING_READBACK,
        StepStatus.VERIFICATION_BLOCKED,
        StepStatus.VERIFICATION_UNKNOWN,
        StepStatus.SUCCEEDED,
    }
)

SampleCandidate = Callable[[RunRecord, StepRecord], bool]


class DailyReviewJobs:
    kind = KIND

    def __init__(
        self,
        *,
        jobs: MaintenanceStore,
        runs: RunStore,
        steps: StepStore,
        audit_store: AuditStore,
        audit: AuditLog,
        reviews: ReviewStore,
        registry: ToolRegistry,
        engine: RuleEngine,
        signer: PolicySigner,
        variables: Callable[[RunRecord], Mapping[str, Any]],
        anchorer: Anchorer,
        clock: Callable[[], datetime],
        key: ReviewKey | None,
        sample: SampleCandidate | None = None,
        rate: float = DEFAULT_RATE,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._jobs, self._runs, self._steps = jobs, runs, steps
        self._events, self._audit, self._reviews = audit_store, audit, reviews
        self._registry, self._engine, self._signer = registry, engine, signer
        self._variables, self._anchorer, self._clock = variables, anchorer, clock
        self._key, self._sample, self._rate = key, sample, rate
        self._telemetry: Telemetry = telemetry or NoopTelemetry()

    # -- scheduling ---------------------------------------------------------------------------

    def schedule(self, now: datetime) -> int:
        """Ensure each tenant has a job for each of the last CATCH_UP_DAYS UTC days, so days
        missed while no scheduler ran are still reviewed. Returns how many were new."""
        today = now.date()
        days = [today - timedelta(days=n) for n in range(1, CATCH_UP_DAYS + 1)]
        return sum(
            self._jobs.add(review_job(tenant, day))
            for tenant in self._runs.tenant_ids()
            for day in days
        )

    # -- one job ------------------------------------------------------------------------------

    def process(self, job: MaintenanceJob) -> str:
        fresh = job.result is None
        if fresh:
            day = date.fromisoformat(str(job.payload["day"]))
            found = self.review(job.tenant_id, day)
            counts = {reason: sum(i.reason == reason for i in found) for reason in _REASONS}
            for reason, count in counts.items():
                if count:
                    self._telemetry.count("gawe.review.items", count, reason=reason)
            job = self._change(
                job,
                result={
                    "outcome": "reviewed",
                    "code": "daily_review",
                    "items": len(found),
                    "sampled": counts["sampled_low"],
                    "sample": "on" if self._key and self._sample else "off",
                },
            )
        if not job.continued:
            job = self._change(job, continued=True)
        return "reviewed" if fresh else "continued"

    def review(self, tenant_id: str, day: date) -> list[ReviewItem]:
        start, end = day_window(day)
        found: list[ReviewItem] = []
        for run_id in self._events.run_ids_with("policy.decided", start, end):
            try:
                run = self._runs.get(run_id)
            except RunNotFoundError:
                continue
            if run.tenant_id != tenant_id:
                continue  # another tenant's run: its own job reviews it
            found.extend(self._review_run(run, day, start, end))
        return found

    # -- one run ------------------------------------------------------------------------------

    def _review_run(
        self, run: RunRecord, day: date, start: datetime, end: datetime
    ) -> list[ReviewItem]:
        events = self._events.events(run.run_id)
        decided = [e for e in events if e.type == "policy.decided"]
        audited = {e.payload_hash for e in decided}
        today = {e.payload_hash for e in decided if start <= e.ts < end}
        found: list[tuple[StepRecord | None, PolicyDecision | None, Reason]] = []
        if not verify_chain(events).ok:
            found.append((None, None, "audit_mismatch"))

        matched: set[str] = set()
        mismatched = 0
        for step in self._steps_of(run):
            decision = step.policy_decision
            if decision is None:
                continue
            digest = sha256_hex(canonical_json(decided_payload(decision)))
            if digest not in audited:
                found.append((step, decision, "audit_mismatch"))
                mismatched += 1
                continue
            matched.add(digest)
            if digest not in today:
                continue  # decided on another day; that day's review covered it
            found.extend((step, decision, r) for r in self._check(run, step, decision))

        unmatched = len(today - matched) - mismatched
        if unmatched > 0:
            found.append((None, None, "superseded" if self._refreshed(run) else "audit_mismatch"))

        before = self._audit.last(run.run_id)
        items = [self._record(run, day, step, decision, reason) for step, decision, reason in found]
        after = self._audit.last(run.run_id)
        if after != before and run.status in RUN_TERMINAL:
            self._anchorer.anchor_run(run)  # extends the run's anchor past the new events
        return items

    def _check(self, run: RunRecord, step: StepRecord, decision: PolicyDecision) -> list[Reason]:
        if not self._signer.verify(decision):
            return ["signature_invalid"]
        recomputed = action_hash(
            tool=step.tool,
            version=step.tool_version,
            resolved_args=step.resolved_args,
            run_id=run.run_id,
            step_id=step.step_id,
        )
        if recomputed != decision.action_hash or step.action_hash != decision.action_hash:
            return ["action_mismatch"]
        if decision.outcome != "allow" or step.status not in EXECUTED:
            return []
        reasons: list[Reason] = []
        changed = self._reevaluate(run, step, decision)
        if changed is not None:
            reasons.append(changed)
        if (
            self._key is not None
            and self._sample is not None
            and self._sample(run, step)
            and self._key.selects(decision.action_hash, self._rate)
        ):
            reasons.append("sampled_low")
        return reasons

    def _reevaluate(
        self, run: RunRecord, step: StepRecord, decision: PolicyDecision
    ) -> Reason | None:
        try:
            manifest = self._registry.get(step.tool, step.tool_version)
        except ToolNotFoundError:
            return None
        if manifest.retired:
            return None  # current rules are not written for retired versions
        now = self._engine.evaluate(
            EvaluationContext(
                run_id=run.run_id,
                step_id=step.step_id,
                manifest=manifest,
                resolved_args=step.resolved_args,
                acting_user_id=run.acting_user_id,
                user_roles=frozenset(run.roles),
                variables=self._variables(run),
            )
        )
        if now.outcome == decision.outcome:
            return None
        if decision.format == 1:
            return "rules_may_have_changed"
        if decision.policy_digest != self._engine.policy_digest:
            return "rules_changed"
        return "inputs_changed"

    def _record(
        self,
        run: RunRecord,
        day: date,
        step: StepRecord | None,
        decision: PolicyDecision | None,
        reason: Reason,
    ) -> ReviewItem:
        step_id = step.step_id if step else None
        digest = decision.action_hash if decision else None
        item = self._reviews.add(
            ReviewItem(
                item_id=item_identity(run.tenant_id, day, run.run_id, step_id, digest, reason),
                tenant_id=run.tenant_id,
                day=day,
                run_id=run.run_id,
                step_id=step_id,
                action_hash=digest,
                reason=reason,
                key_id=self._key.key_id if reason == "sampled_low" and self._key else None,
                created_at=self._clock(),
            )
        )
        if item.event_seq is None:
            event = self._audit.record(
                run.run_id,
                "review.sampled" if reason == "sampled_low" else "review.flagged",
                ACTOR,
                {
                    "item_id": item.item_id,
                    "day": day.isoformat(),
                    "step_id": step_id,
                    "action_hash": digest,
                    "reason": reason,
                    "key_id": item.key_id,
                },
            )
            item = self._reviews.set_event(run.tenant_id, item.item_id, event.seq)
        return item

    def _steps_of(self, run: RunRecord) -> list[StepRecord]:
        steps: list[StepRecord] = []
        for planned in run.plan.steps if run.plan else ():
            try:
                steps.append(self._steps.get(run.run_id, planned.id))
            except StepNotFoundError:
                continue
        return steps

    def _refreshed(self, run: RunRecord) -> bool:
        return any(
            j.kind == READBACK_KIND
            and j.result is not None
            and j.result.get("outcome") == "recipient_changed"
            for j in self._jobs.for_run(run.tenant_id, run.run_id)
        )

    def _change(self, job: MaintenanceJob, **updates: object) -> MaintenanceJob:
        changed = MaintenanceJob(**{**job.model_dump(), **updates, "revision": job.revision + 1})
        return self._jobs.replace(changed, expected_revision=job.revision)


_REASONS: tuple[Reason, ...] = (
    "audit_mismatch",
    "superseded",
    "signature_invalid",
    "action_mismatch",
    "rules_changed",
    "rules_may_have_changed",
    "inputs_changed",
    "sampled_low",
)
