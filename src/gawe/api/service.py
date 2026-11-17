"""Everything the API does, with no web framework in sight.

Every method takes the caller's `Identity` and enforces, in this order: the tenant boundary
(another tenant's run or approval is reported as not found), then the caller's role or ownership.
The routes only translate HTTP to these calls, so the rules are tested here once.

Who may do what:
* start a run, list/view runs, view a run's audit trail: any authenticated user in the tenant;
* cancel or resume a run: the user who started it, or anyone with the `admin` role;
* see an approval in the inbox: someone holding the approver role the policy named, who did not
  request it themselves;
* list next-day review items and give a verdict: the `legal_reviewer` role, and never on a run
  the reviewer started;
* decide an approval: the approval service's rules (hash match, expiry, separation of duties,
  role); this layer only adds the tenant check and maps failures to API errors.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gawe.api.auth import Identity
from gawe.api.background import Background
from gawe.api.errors import (
    ApiError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from gawe.api.schemas import (
    AnchorView,
    ApprovalView,
    AuditEventView,
    AuditVerificationView,
    ChangeView,
    CheckView,
    DifferenceView,
    EventsPage,
    FreeTextFieldView,
    ReviewItemView,
    RunView,
    StepView,
    ToolView,
    TypedFieldView,
    VerificationView,
)
from gawe.core.anchor import Anchorer
from gawe.core.approvals import (
    ActionHashMismatchError,
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalRecord,
    ApprovalService,
    ApprovalStore,
    ApproverNotAuthorizedError,
    Decision,
    Principal,
    SeparationOfDutiesError,
)
from gawe.core.audit import AnchorPoint, AuditLog
from gawe.core.fields import FREE_TEXT_LABEL, difference, fields, length
from gawe.core.maintenance import MaintenanceStore, run_target
from gawe.core.models import AuditEvent
from gawe.core.records import StepNotFoundError, StepRecord, StepStore
from gawe.core.registry import ToolNotFoundError, ToolRegistry
from gawe.core.review import (
    InMemoryReviewStore,
    ReviewAlreadyDecidedError,
    ReviewItem,
    ReviewItemNotFoundError,
    ReviewStore,
    Status,
    Verdict,
)
from gawe.core.runs import SOURCE_ID_PATTERN, RunNotFoundError, RunRecord, RunStore
from gawe.core.sources import DocumentSource, SourceError
from gawe.core.statemachine import RUN_TERMINAL, StepStatus
from gawe.core.verification import READBACK_KIND
from gawe.engine.orchestrator import Orchestrator, RunBusyError
from gawe.engine.recovery import resume_interrupted
from gawe.llm.planner import MAX_OBJECTIVE_CHARS
from gawe.maintenance.runner import MaintenanceRunner

ADMIN_ROLE = "admin"
AUDIT_ACTOR = "gawe.api"


REVIEWER_ROLE = "legal_reviewer"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Limits:
    max_document_chars: int = 200_000
    max_list: int = 100
    max_comment_chars: int = 2000


class ApiService:
    def __init__(
        self,
        *,
        orchestrator: Orchestrator,
        runs: RunStore,
        steps: StepStore,
        approval_service: ApprovalService,
        approval_store: ApprovalStore,
        registry: ToolRegistry,
        audit: AuditLog,
        background: Background,
        profiles: frozenset[str],
        limits: Limits | None = None,
        anchorer: Anchorer | None = None,
        maintenance: MaintenanceStore | None = None,
        maintenance_runner: MaintenanceRunner | None = None,
        reviews: ReviewStore | None = None,
        sources: Mapping[str, DocumentSource] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._orch = orchestrator
        self._runs = runs
        self._steps = steps
        self._approvals = approval_service
        self._approval_store = approval_store
        self._registry = registry
        self._audit = audit
        self._background = background
        self._profiles = profiles
        self._limits = limits or Limits()
        self._anchorer = anchorer or Anchorer(audit, [])
        self._maintenance = maintenance
        self._reviews = reviews or InMemoryReviewStore()
        self._sources = dict(sources or {})
        self._clock = clock
        self.maintenance_runner = maintenance_runner

    # -- tools ----------------------------------------------------------------------------

    def list_tools(self, who: Identity) -> list[ToolView]:
        return [
            ToolView(
                name=m.name,
                version=m.version,
                description=m.description,
                side_effect_class=m.side_effect_class,
                risk_tier=m.risk_tier,
                required_roles=m.required_roles,
                input_schema=m.proposal_schema or m.input_schema,
            )
            for m in sorted(self._registry.list(), key=lambda m: m.name)
        ]

    # -- runs -----------------------------------------------------------------------------

    def start_run(
        self,
        who: Identity,
        *,
        objective: str,
        profile: str,
        document: str | None = None,
        source_id: str | None = None,
    ) -> RunView:
        """A profile with a source (IT access) takes `source_id`, never an uploaded document:
        the server fetches the text from that record in the caller's tenant."""
        if not objective.strip():
            raise BadRequestError("objective is required")
        if len(objective) > MAX_OBJECTIVE_CHARS:
            raise BadRequestError(f"objective is longer than {MAX_OBJECTIVE_CHARS} characters")
        if profile not in self._profiles:
            raise BadRequestError("unknown profile")
        source = self._sources.get(profile)
        if source is None and source_id is not None:
            raise BadRequestError("this profile does not read from a source record")
        if source is not None:
            if document is not None:
                raise BadRequestError("this profile reads its document from its source record")
            if source_id is None:
                raise BadRequestError("source_id is required for this profile")
            if re.fullmatch(SOURCE_ID_PATTERN, source_id) is None:
                raise BadRequestError("source_id is malformed")
            try:
                document = source.fetch(who.tenant_id, source_id)
            except SourceError as exc:
                raise BadRequestError(f"source record unavailable: {exc.code}") from None
        if document is not None and len(document) > self._limits.max_document_chars:
            raise BadRequestError(
                f"document is longer than {self._limits.max_document_chars} characters"
            )
        try:
            run = self._orch.start_run(
                tenant_id=who.tenant_id,
                acting_user_id=who.user_id,
                roles=tuple(sorted(who.roles)),
                objective=objective,
                profile=profile,
                document=document,
                source_id=source_id,
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        self.kick(run.run_id)
        return self._run_view(run)

    def get_run(self, who: Identity, run_id: str) -> RunView:
        return self._run_view(self._own_run(who, run_id))

    def verification_evidence(self, who: Identity, run_id: str) -> list[VerificationView]:
        run = self._own_run(who, run_id)
        return self._verification_views(run)

    def _verification_views(self, run: RunRecord) -> list[VerificationView]:
        if self._maintenance is None:
            return []
        return [
            VerificationView(
                job_id=j.job_id,
                step_id=run_target(j).step_id,
                plan_version=run_target(j).plan_version,
                operation_key=j.operation_key,
                action_hash=run_target(j).action_hash,
                deadline=j.deadline,
                due_at=j.due_at,
                outcome=j.result.get("outcome") if j.result else None,
                code=j.result.get("code") if j.result else j.evidence.get("code"),
                protocol_error=j.protocol_error,
                evidence=j.evidence,
            )
            for j in self._maintenance.for_run(run.tenant_id, run.run_id)
            if j.kind == READBACK_KIND
        ]

    def list_runs(self, who: Identity, *, limit: int = 50) -> list[RunView]:
        limit = max(1, min(limit, self._limits.max_list))
        return [self._run_view(r) for r in self._runs.list_for_tenant(who.tenant_id, limit=limit)]

    def cancel_run(self, who: Identity, run_id: str) -> RunView:
        run = self._own_run(who, run_id)
        self._require_owner_or_admin(who, run)
        try:
            return self._run_view(self._orch.cancel(run_id, by=who.user_id))
        except RunBusyError as exc:
            raise ConflictError(str(exc)) from exc

    def resume_run(self, who: Identity, run_id: str) -> RunView:
        """Ask for the run to be driven again (for example after a server restart)."""
        run = self._own_run(who, run_id)
        self._require_owner_or_admin(who, run)
        pending_evidence = self._maintenance is not None and any(
            not j.continued and j.kind == READBACK_KIND
            for j in self._maintenance.for_run(run.tenant_id, run_id)
        )
        if run.status not in RUN_TERMINAL or pending_evidence:
            self.kick(run_id)
        return self._run_view(run)

    # -- approvals ------------------------------------------------------------------------

    def inbox(self, who: Identity) -> list[ApprovalView]:
        """Open approvals this person could decide right now."""
        views: list[ApprovalView] = []
        for record in self._approval_store.open_approvals():
            if record.approver_role not in who.roles or record.requested_by == who.user_id:
                continue
            run = self._find_run(record.run_id)
            if run is None or run.tenant_id != who.tenant_id:
                continue
            step = self._find_step(record.run_id, record.step_id)
            if step is None or step.status is not StepStatus.AWAITING_APPROVAL:
                continue  # the run ended or moved on; the approval is stale
            views.append(self._approval_view(record, run, step))
        return views[: self._limits.max_list]

    def decide(
        self,
        who: Identity,
        approval_id: str,
        *,
        decision: Decision,
        action_hash: str,
        comment: str | None = None,
    ) -> ApprovalView:
        if comment is not None and len(comment) > self._limits.max_comment_chars:
            raise BadRequestError("comment is too long")
        try:
            record = self._approval_store.get(approval_id)
        except ApprovalNotFoundError:
            raise NotFoundError("approval not found") from None
        run = self._find_run(record.run_id)
        if run is None or run.tenant_id != who.tenant_id:
            raise NotFoundError("approval not found")
        try:
            decided = self._approvals.decide(
                approval_id,
                decider=Principal(who.user_id, who.roles),
                decision=decision,
                action_hash=action_hash,
                comment=comment,
            )
        except (SeparationOfDutiesError, ApproverNotAuthorizedError) as exc:
            raise ForbiddenError(str(exc)) from exc
        except ActionHashMismatchError as exc:
            raise ConflictError(str(exc)) from exc
        except (ApprovalExpiredError, ApprovalAlreadyDecidedError) as exc:
            raise ConflictError(str(exc)) from exc
        self.kick(decided.run_id)
        step = self._find_step(decided.run_id, decided.step_id)
        if step is None:
            raise NotFoundError("approval not found")
        return self._approval_view(decided, run, step)

    # -- next-day review -----------------------------------------------------------------

    def review_items(
        self, who: Identity, *, status: Status | None = "open"
    ) -> list[ReviewItemView]:
        """The tenant's review items, for someone who may give a verdict."""
        self._require_reviewer(who)
        items = self._reviews.list(who.tenant_id, status=status, limit=self._limits.max_list)
        return [_review_view(i) for i in items]

    def review_verdict(self, who: Identity, item_id: str, *, verdict: Verdict) -> ReviewItemView:
        """Record `confirmed` or `missed_risk`, once, by a reviewer who did not start the run."""
        self._require_reviewer(who)
        try:
            item = self._reviews.get(who.tenant_id, item_id)
        except ReviewItemNotFoundError:
            raise NotFoundError("review item not found") from None
        run = self._find_run(item.run_id)
        if run is None or run.tenant_id != who.tenant_id:
            raise NotFoundError("review item not found")
        if run.acting_user_id == who.user_id:
            raise ForbiddenError("the person who started a run cannot review it")
        try:
            decided = self._reviews.decide(
                who.tenant_id,
                item_id,
                verdict=verdict,
                decided_by=who.user_id,
                decided_at=self._clock(),
            )
        except ReviewAlreadyDecidedError as exc:
            raise ConflictError(str(exc)) from exc
        return _review_view(decided)

    @staticmethod
    def _require_reviewer(who: Identity) -> None:
        if REVIEWER_ROLE not in who.roles:
            raise ForbiddenError(f"reviewing needs the {REVIEWER_ROLE} role")

    # -- audit ----------------------------------------------------------------------------

    def audit_events(self, who: Identity, run_id: str) -> list[AuditEventView]:
        self._own_run(who, run_id)
        return [_event_view(e) for e in self._audit.events(run_id)]

    def verify_audit(
        self, who: Identity, run_id: str, *, anchor: AnchorPoint | None = None
    ) -> AuditVerificationView:
        """Check the chain, and against `anchor` (the caller's own copy) or the stored anchor.

        A mismatched or invalid anchor makes `ok` false. Hashes the caller sent are never echoed.
        """
        run = self._own_run(who, run_id)
        try:
            check = self._anchorer.verify(run, caller=anchor)
        except ValueError:
            raise BadRequestError(
                "anchor must be a positive sequence number and a lowercase SHA-256 hash"
            ) from None
        chain = check.chain
        record, evidence = check.record, check.evidence
        if record is not None:
            seq: int | None = record.point.seq
        else:
            seq = anchor.seq if anchor is not None else None
        return AuditVerificationView(
            ok=check.ok,
            checked=chain.checked,
            head=chain.head,
            failed_seq=chain.failed_seq,
            reason=chain.reason,
            anchor=AnchorView(
                status=check.status,
                source=check.source,
                sink=record.sink if record is not None else None,
                external=evidence.external if evidence is not None else False,
                seq=seq,
                anchored_at=record.anchored_at if record is not None else None,
                timestamped_at=evidence.timestamped_at if evidence is not None else None,
                events_after_anchor=chain.events_after_anchor,
            ),
        )

    def events_after(
        self, who: Identity, run_id: str, *, after_seq: int = 0, limit: int = 200
    ) -> EventsPage:
        run = self._own_run(who, run_id)
        events = [e for e in self._audit.events(run_id) if e.seq > after_seq][:limit]
        finished = run.status in RUN_TERMINAL and len(events) < limit
        return EventsPage(
            events=tuple(_event_view(e) for e in events),
            run_status=run.status.value,
            finished=finished,
        )

    # -- internals ------------------------------------------------------------------------

    def _own_run(self, who: Identity, run_id: str) -> RunRecord:
        run = self._find_run(run_id)
        if run is None or run.tenant_id != who.tenant_id:
            raise NotFoundError("run not found")
        return run

    def _find_run(self, run_id: str) -> RunRecord | None:
        try:
            return self._runs.get(run_id)
        except RunNotFoundError:
            return None
        except ValueError:  # a malformed id cannot name a run
            return None

    def _find_step(self, run_id: str, step_id: str) -> StepRecord | None:
        try:
            return self._steps.get(run_id, step_id)
        except StepNotFoundError:
            return None

    @staticmethod
    def _require_owner_or_admin(who: Identity, run: RunRecord) -> None:
        if run.acting_user_id != who.user_id and ADMIN_ROLE not in who.roles:
            raise ForbiddenError("only the person who started the run, or an admin, can do this")

    def recover_interrupted(self) -> list[str]:
        """Drive every unfinished run again. Call once at startup; safe to call repeatedly."""
        return resume_interrupted(self._runs, self.kick)

    def kick(self, run_id: str) -> None:
        """Drive the run on the background pool (resume, recovery, scheduled continuation)."""
        self._background.submit(lambda: self._advance(run_id))

    def _advance(self, run_id: str) -> None:
        try:
            self._orch.advance(run_id)
        except ApiError:
            raise
        except Exception as exc:
            # Leave the run where it is (it can be resumed); record only the error class.
            self._audit.record(
                run_id, "run.advance_error", AUDIT_ACTOR, {"error": type(exc).__name__}
            )
            run = self._runs.get(run_id)
            if run.status in RUN_TERMINAL:
                self._anchorer.anchor_run(run)

    def _run_view(self, run: RunRecord) -> RunView:
        steps: list[StepView] = []
        evidence = self._verification_views(run)
        for planned in run.plan.steps if run.plan else ():
            step = self._find_step(run.run_id, planned.id)
            if step is None:
                continue
            decision = step.policy_decision or run.decisions.get(step.step_id)
            steps.append(
                StepView(
                    step_id=step.step_id,
                    tool=step.tool,
                    status=step.status.value,
                    depends_on=step.depends_on,
                    policy_outcome=decision.outcome if decision else None,
                    policy_rule_ids=decision.rule_ids if decision else (),
                    policy_checks=tuple(
                        CheckView(rule_id=c.rule_id, path=c.path, op=c.op, unreadable=c.unreadable)
                        for c in (decision.checks if decision else ())
                    ),
                    approver_role=decision.approver_role if decision else None,
                    action_hash=step.action_hash,
                    error=step.error,
                    verification=next(
                        (
                            v
                            for v in evidence
                            if v.step_id == step.step_id
                            and v.plan_version == run.plan_version
                            and v.action_hash == step.action_hash
                        ),
                        None,
                    ),
                )
            )
        return RunView(
            run_id=run.run_id,
            objective=run.objective,
            profile=run.profile,
            source_id=run.source_id,
            status=run.status.value,
            error=run.error,
            acting_user_id=run.acting_user_id,
            plan_version=run.plan_version,
            created_at=run.created_at,
            completed_at=run.completed_at,
            steps=tuple(steps),
        )

    def _approval_view(
        self, record: ApprovalRecord, run: RunRecord, step: StepRecord
    ) -> ApprovalView:
        decision = step.policy_decision or run.decisions.get(step.step_id)
        manifest = self._registry.get(step.tool, step.tool_version)
        snapshot = record.resolved_args or step.resolved_args
        classes = manifest.field_classes or {}
        shown = fields(snapshot, classes)
        return ApprovalView(
            approval_id=record.approval_id,
            run_id=record.run_id,
            step_id=record.step_id,
            tool=step.tool,
            risk_tier=manifest.risk_tier,
            resolved_args=record.resolved_args or step.resolved_args,
            preparation=record.preparation,
            action_hash=record.action_hash,
            approver_role=record.approver_role,
            reason=decision.reason if decision else "",
            requested_by=record.requested_by,
            requested_at=record.requested_at,
            expires_at=record.expires_at,
            decision=record.decision,
            decided_by=record.decided_by,
            typed_fields=tuple(
                TypedFieldView(path=f.path, value=f.value) for f in shown if f.kind == "typed"
            ),
            free_text_fields=tuple(
                FreeTextFieldView(
                    path=f.path, label=FREE_TEXT_LABEL, length=length(f.value), content=f.value
                )
                for f in shown
                if f.kind == "free_text"
            ),
            difference=self._difference(record, run, step, snapshot, classes),
        )

    def _difference(
        self,
        record: ApprovalRecord,
        run: RunRecord,
        step: StepRecord,
        snapshot: Mapping[str, Any],
        classes: Mapping[str, str],
    ) -> DifferenceView:
        previous = self._approval_store.latest_approved(
            tenant_id=run.tenant_id,
            tool=step.tool,
            exclude_id=record.approval_id,
            decided_before=record.decided_at,
        )
        if previous is None:
            return DifferenceView()
        try:
            earlier = self._registry.get(step.tool, previous.tool_version).field_classes or {}
        except ToolNotFoundError:
            earlier = {}  # unknown version: every earlier value counts as free text
        changes = difference(snapshot, classes, previous.record.resolved_args, earlier)
        return DifferenceView(
            previous_approval_id=previous.record.approval_id,
            previous_decided_at=previous.record.decided_at,
            changes=tuple(
                ChangeView(
                    path=c.path, kind=c.kind, change=c.change, before=c.before, after=c.after
                )
                for c in changes
            ),
        )


def _review_view(item: ReviewItem) -> ReviewItemView:
    return ReviewItemView(
        item_id=item.item_id,
        day=item.day,
        run_id=item.run_id,
        step_id=item.step_id,
        action_hash=item.action_hash,
        reason=item.reason,
        key_id=item.key_id,
        status=item.status,
        created_at=item.created_at,
        decided_by=item.decided_by,
        decided_at=item.decided_at,
    )


def _event_view(event: AuditEvent) -> AuditEventView:
    return AuditEventView(
        seq=event.seq,
        type=event.type,
        actor=event.actor,
        payload_hash=event.payload_hash,
        hash=event.hash,
        ts=event.ts,
    )
