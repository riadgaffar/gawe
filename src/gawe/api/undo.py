"""Trusted undo requests, durable before run creation, replayable without model calls."""

from collections import Counter
from datetime import datetime, timedelta

from jsonschema import Draft202012Validator

from gawe.api.auth import Identity
from gawe.api.errors import ConflictError, ForbiddenError
from gawe.core.canonical import action_hash, sha256_hex
from gawe.core.history import SubjectLocks
from gawe.core.maintenance import MaintenanceJob
from gawe.core.models import Plan, PlanStep
from gawe.core.records import StepNotFoundError, StepRecord
from gawe.core.runs import RunNotFoundError, RunRecord
from gawe.core.statemachine import RUN_TERMINAL
from gawe.core.telemetry import NoopTelemetry, Telemetry
from gawe.core.undo import (
    REASONS,
    UndoRecord,
    legacy_restore_version,
    reversal_key,
    reversal_run_id,
)
from gawe.core.verification import VerificationResolver
from gawe.engine.orchestrator import Orchestrator


class UndoRefused(ConflictError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# A bounded vocabulary: arbitrary exception text or stored content never becomes a metric label.
UNAVAILABLE_REASONS = frozenset(
    {
        *REASONS.values(),
        "result_version_unobserved",
        "record_changed",
        "cancelled",
        "denied",
        "other",
    }
)


class UndoRuns:
    kind = "undo_request"

    def __init__(
        self,
        resolver: VerificationResolver,
        orchestrator: Orchestrator,
        locks: SubjectLocks,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.r, self.orchestrator, self.locks = resolver, orchestrator, locks
        self.telemetry = telemetry or NoopTelemetry()

    def schedule(self, now: datetime) -> int:
        """Observe a counts-only snapshot each maintenance tick; this schedules no work.

        Failed reversals are unavailable too. Pending and requested records are in flight and
        omitted. Zeroes clear previous buckets; repeated ticks are observations, not increments.
        A failed read raises, so maintenance reports an error instead of fabricated zeroes.
        """
        for tenant in self.r.runs.tenant_ids():
            counts: Counter[tuple[str, str]] = Counter()
            for (state, reason), value in self.r.undo.counts(tenant).items():
                if state in {"armed", "used"}:
                    counts[state, ""] += value
                elif state in {"unavailable", "failed"}:
                    counts[
                        "unavailable",
                        reason if reason is not None and reason in UNAVAILABLE_REASONS else "other",
                    ] += value
            buckets = [("armed", ""), ("used", "")]
            buckets += [("unavailable", reason) for reason in sorted(UNAVAILABLE_REASONS)]
            for state, reason in buckets:
                self.telemetry.observe(
                    "gawe.undo.records",
                    counts[state, reason],
                    tenant_id=tenant,
                    state=state,
                    reason=reason,
                )
        return 0

    def request(self, who: Identity, run: RunRecord, step: StepRecord) -> RunRecord:
        if "case_manager" not in who.roles:
            raise ForbiddenError("undo requires case_manager")
        spec = self.r.undoable.get((step.tool, step.tool_version))
        is_undo = run.profile == "undo"
        if spec is None and not is_undo:
            raise UndoRefused("not_reversible")
        with self.locks.hold(f"undo:{run.tenant_id}:{run.run_id}:{step.step_id}"):
            try:
                records = self.r.undo.for_run(run.tenant_id, run.run_id)
            except Exception as exc:
                raise UndoRefused("undo_unavailable") from exc
            record = next(
                (
                    v
                    for v in records
                    if v.step_id == step.step_id and v.action_hash == step.action_hash
                ),
                None,
            )
            if not is_undo and (record is None or record.state in {"pending", "unavailable"}):
                raise UndoRefused("undo_unavailable")
            if record is not None and (
                record.state in {"requested", "used", "failed"}
                or self.r.jobs.get(run.tenant_id, reversal_key(record.operation_key)) is not None
            ):
                raise UndoRefused("already_undone")
            if is_undo:
                raise UndoRefused("undo_of_undo")
            assert record is not None and spec is not None
            try:
                manifest = self.r.registry.get(spec.restore_tool)
                args = self.r.restore_arguments(record, spec.restore_tool, manifest.version)
                if not Draft202012Validator(manifest.input_schema).is_valid(args):
                    raise ValueError("unreadable undo arguments")
            except Exception as exc:
                raise UndoRefused("undo_unavailable") from exc
            now = self.r.clock()
            job = MaintenanceJob(
                job_id=reversal_key(record.operation_key),
                kind=self.kind,
                tenant_id=run.tenant_id,
                run_id=run.run_id,
                plan_version=run.plan_version,
                step_id=step.step_id,
                action_hash=record.action_hash,
                operation_key=reversal_key(record.operation_key),
                created_at=now,
                due_at=now,
                deadline=now + timedelta(days=36500),
                payload={
                    "undo_id": record.undo_id,
                    "user_id": who.user_id,
                    "roles": sorted(who.roles),
                    "tool": spec.restore_tool,
                    "tool_version": manifest.version,
                },
            )
            # The durable intent is the request's linearization point. Recovery owns it now.
            if not self.r.jobs.add(job):
                raise UndoRefused("already_undone")
            return self.ensure(job)

    def ensure(self, job: MaintenanceJob) -> RunRecord:
        record = self.r.undo.get(job.tenant_id, job.payload["undo_id"])
        if record is None:
            raise ValueError("undo record missing")
        run_id = reversal_run_id(record)
        tool = job.payload["tool"]
        # Requests written before M27 pinned the M26 restore implicitly, not "latest".
        version = job.payload.get("tool_version") or legacy_restore_version(tool)
        manifest = self.r.registry.get(tool, version)
        args = self.r.restore_arguments(record, tool, version)
        if not manifest.trusted_only:
            raise ValueError("restore must be trusted-only")
        plan = Plan(steps=(PlanStep(id="restore", tool=tool, tool_version=version, args=args),))
        digest = action_hash(
            tool=tool,
            version=manifest.version,
            resolved_args=args,
            run_id=run_id,
            step_id="restore",
        )
        self.r.audit.record(
            record.run_id,
            "step.undo_requested",
            job.payload["user_id"],
            {
                "step_id": record.step_id,
                "action_hash": record.action_hash,
                "undo_run_id": run_id,
                "undo_step_id": "restore",
                "undo_action_hash": digest,
            },
            once=True,
        )
        if record.state == "armed":
            self.r.undo.replace(
                UndoRecord(
                    **{**record.model_dump(), "state": "requested", "revision": record.revision + 1}
                ),
                expected_revision=record.revision,
            )
        try:
            run = self.r.runs.get(run_id)
        except RunNotFoundError:
            run = self.orchestrator.start_run(
                tenant_id=record.tenant_id,
                acting_user_id=job.payload["user_id"],
                roles=tuple(job.payload["roles"]),
                objective="Undo an earlier rating",
                profile="undo",
                run_id=run_id,
            )
        # Repair a crash after the run insert but before start_run appended its first event.
        self.r.audit.record(
            run_id,
            "run.created",
            run.acting_user_id,
            {
                "profile": "undo",
                "has_document": False,
                "objective_sha256": sha256_hex(run.objective.encode("utf-8")),
            },
            once=True,
        )
        if run.plan is None:
            run = self.r.runs.update(run_id, plan=plan, plan_version=1)
        elif run.plan != plan:
            # Compare an old plan using its fixed legacy meaning without rewriting its hash,
            # plan version, persisted arguments, or audit events.
            normalized = Plan(
                steps=tuple(
                    step.model_copy(
                        update={
                            "tool_version": step.tool_version or legacy_restore_version(step.tool)
                        }
                    )
                    for step in run.plan.steps
                )
            )
            if normalized != plan:
                raise ValueError("undo plan changed")
        return run

    def process(self, job: MaintenanceJob) -> str:
        with self.locks.hold(f"undo:{job.tenant_id}:{job.run_id}:{job.step_id}"):
            run = self.ensure(job)
        run = self.orchestrator.advance(run.run_id)
        self.finished(run)
        return "continued" if run.status in RUN_TERMINAL else "still_waiting"

    def finished(self, run: RunRecord) -> None:
        if run.profile != "undo" or run.status not in RUN_TERMINAL or run.plan is None:
            return
        record = self.r.undo.get(run.tenant_id, run.plan.steps[0].args["undo_id"])
        if record is None:
            raise ValueError("undo record missing")
        try:
            step = self.r.steps.get(run.run_id, "restore")
        except StepNotFoundError:
            # A user may cancel the new run before the driver materializes its trusted plan.
            planned = run.plan.steps[0]
            manifest = self.r.registry.get(
                planned.tool, planned.tool_version or legacy_restore_version(planned.tool)
            )
            step = StepRecord(
                run_id=run.run_id,
                step_id=planned.id,
                tool=planned.tool,
                tool_version=manifest.version,
                action_hash=action_hash(
                    tool=planned.tool,
                    version=manifest.version,
                    resolved_args=planned.args,
                    run_id=run.run_id,
                    step_id=planned.id,
                ),
            )
        success = record.state == "used"
        code = None if success else record.reason or step.error or run.error or run.status.value
        if record.state == "requested":
            self.r.undo.replace(
                UndoRecord(
                    **{
                        **record.model_dump(),
                        "state": "failed",
                        "reason": code,
                        "revision": record.revision + 1,
                    }
                ),
                expected_revision=record.revision,
            )
        self.r.audit.record(
            record.run_id,
            "step.undone" if success else "step.undo_failed",
            "gawe.undo",
            {
                "step_id": record.step_id,
                "action_hash": record.action_hash,
                "undo_run_id": run.run_id,
                "undo_step_id": step.step_id,
                "undo_action_hash": step.action_hash,
                "code": code,
            },
            once=True,
        )
        job = self.r.jobs.get(run.tenant_id, reversal_key(record.operation_key))
        if job is not None and not job.continued:
            self.r.change(
                job,
                result={"outcome": "done" if success else "failed", "code": code},
                continued=True,
                lease_owner=None,
                lease_until=None,
            )
