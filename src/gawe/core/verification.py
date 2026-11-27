"""One resolver for immediate, resumed and eventually scheduled write verification.

Completion is deliberately recoverable across stores: save the immutable result first, apply the
step transition second, append its evidence digest third. The driver acknowledges continuation
only after settling the run and anchoring. A crash repeats bookkeeping, never the external write.
"""

import contextlib
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator

from gawe.core.audit import AuditLog
from gawe.core.canonical import action_hash, canonical_json, sha256_hex
from gawe.core.expiry import expiry_job
from gawe.core.maintenance import JobConflictError, MaintenanceJob, MaintenanceStore, run_target
from gawe.core.readback import ReadbackAdapter, ReadbackResult, job_identity, operation_identity
from gawe.core.record_changes import RecordObservation
from gawe.core.records import (
    AttemptConflictError,
    ExecutionStore,
    StaleStateError,
    StepRecord,
    StepStore,
)
from gawe.core.refusals import REFUSAL_CODES, RefusalObserver
from gawe.core.registry import ToolRegistry
from gawe.core.runs import RunStore
from gawe.core.statemachine import STEP_TERMINAL, RunStatus, StepStatus
from gawe.core.undo import (
    InMemoryUndoStore,
    Undoable,
    UndoConflictError,
    UndoRecord,
    UndoStore,
    legacy_restore_version,
    restore_args,
    reversal_key,
    reversal_run_id,
    settled,
    undo_identity,
)

READBACK_KIND = "write_readback"
VERIFICATION_WINDOW = timedelta(minutes=5)
JOB_LEASE = timedelta(seconds=30)
_RESULT_STATES = {
    "agreement": StepStatus.SUCCEEDED,
    "disagreement": StepStatus.VERIFICATION_BLOCKED,
    "unknown": StepStatus.VERIFICATION_UNKNOWN,
    "protocol_failure": StepStatus.FAILED,
    "not_dispatched": StepStatus.FAILED,
    "recipient_changed": StepStatus.READY,
}


class VerificationResolver:
    def __init__(
        self,
        *,
        jobs: MaintenanceStore,
        steps: StepStore,
        executions: ExecutionStore,
        runs: RunStore,
        registry: ToolRegistry,
        adapters: Mapping[tuple[str, str], ReadbackAdapter],
        audit: AuditLog,
        clock: Callable[[], datetime],
        window: timedelta = VERIFICATION_WINDOW,
        expiring: frozenset[tuple[str, str]] = frozenset(),
        undoable: Mapping[tuple[str, str], Undoable] | None = None,
        undo: UndoStore | None = None,
        refusal_observers: Mapping[tuple[str, str], RefusalObserver] | None = None,
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("verification window must be positive")
        self.window = window
        self.refusal_observers = dict(refusal_observers or {})
        # (tool, version) writes that grant time-bound access: each gets an expiry job, added
        # before its dispatch intent.
        self.expiring = expiring
        # (tool, version) writes that get an undo record before their dispatch intent (M26).
        self.undoable = dict(undoable or {})
        self.undo: UndoStore = undo if undo is not None else InMemoryUndoStore()
        self.jobs, self.steps, self.executions, self.runs = jobs, steps, executions, runs
        self.registry, self.adapters, self.audit, self.clock = (
            registry,
            dict(adapters),
            audit,
            clock,
        )

    def operation_key(self, step: StepRecord) -> str:
        run = self.runs.get(step.run_id)
        assert step.action_hash is not None
        if run.profile != "undo":
            return operation_identity(step.action_hash, run.plan_version)
        record = self.undo.get(run.tenant_id, step.resolved_args.get("undo_id", ""))
        if (
            record is None
            or reversal_run_id(record) != run.run_id
            or record.state not in {"requested", "used", "failed"}
            or step.tool != self.undoable[(record.tool, record.tool_version)].restore_tool
        ):
            raise JobConflictError("undo identity changed")
        request = self.jobs.get(run.tenant_id, reversal_key(record.operation_key))
        if request is None or step.tool_version != (
            request.payload.get("tool_version") or legacy_restore_version(step.tool)
        ):
            raise JobConflictError("undo tool version changed")
        if self.restore_arguments(record, step.tool, step.tool_version) != step.resolved_args:
            raise JobConflictError("undo identity changed")
        return reversal_key(record.operation_key)

    def restore_arguments(self, record: UndoRecord, tool: str, version: str) -> dict[str, Any]:
        manifest = self.registry.get(tool, version)
        return restore_args(
            record, include_snapshots="snapshots" in manifest.input_schema["properties"]
        )

    def find(self, step: StepRecord) -> MaintenanceJob | None:
        run = self.runs.get(step.run_id)
        assert step.action_hash is not None
        key = self.operation_key(step)
        return self.jobs.get(
            run.tenant_id,
            job_identity(
                run.tenant_id,
                run.run_id,
                run.plan_version,
                step.step_id,
                step.action_hash,
                key,
            ),
        )

    def prepare(self, step: StepRecord) -> tuple[MaintenanceJob, bool]:
        """The insert winner alone may dispatch. Existing intent means reconcile only.

        A grant's expiry job is added first, so a dispatched grant always has one; adding it
        again is a no-op."""
        if (step.tool, step.tool_version) in self.expiring:
            self.jobs.add(expiry_job(step, self.runs.get(step.run_id), self.clock()))
        if (step.tool, step.tool_version) in self.undoable:
            self.undo.add(self._undo_record(step))
        old = self.find(step)
        if old:
            return old, False
        run = self.runs.get(step.run_id)
        assert step.action_hash is not None
        now = self.clock()
        key = self.operation_key(step)
        job = MaintenanceJob(
            job_id=job_identity(
                run.tenant_id, run.run_id, run.plan_version, step.step_id, step.action_hash, key
            ),
            kind=READBACK_KIND,
            tenant_id=run.tenant_id,
            run_id=run.run_id,
            plan_version=run.plan_version,
            step_id=step.step_id,
            action_hash=step.action_hash,
            operation_key=key,
            created_at=now,
            deadline=now + self.window,
            due_at=now,
            payload={"tool": step.tool, "version": step.tool_version},
            lease_owner=uuid4().hex,
            lease_until=now + JOB_LEASE,
        )
        return job, self.jobs.add(job)

    def _undo_record(self, step: StepRecord) -> UndoRecord:
        """The undo record for an undoable write about to be dispatched, from its prepared,
        hashed arguments. Its id is deterministic, so adding it again is a no-op."""
        run = self.runs.get(step.run_id)
        assert step.action_hash is not None
        spec = self.undoable[(step.tool, step.tool_version)]
        args = step.resolved_args
        if step.action_hash != action_hash(
            tool=step.tool,
            version=step.tool_version,
            resolved_args=args,
            run_id=step.run_id,
            step_id=step.step_id,
        ):
            raise JobConflictError("undo source action changed")
        identity = undo_identity(run.tenant_id, run.run_id, step.step_id, step.action_hash)
        existing = self.undo.get(run.tenant_id, identity)
        # An old record remains snapshotless, even if its original step has a newer schema.
        snapshots = () if existing is not None and not existing.snapshots else spec.snapshots(args)
        return UndoRecord(
            undo_id=identity,
            snapshots=snapshots,
            tenant_id=run.tenant_id,
            run_id=run.run_id,
            step_id=step.step_id,
            tool=step.tool,
            tool_version=step.tool_version,
            action_hash=step.action_hash,
            operation_key=operation_identity(step.action_hash, run.plan_version),
            subject=spec.subject(args),
            prior=spec.prior(args),
            bound_version=spec.bound_version(args),
            created_at=existing.created_at if existing is not None else self.clock(),
        )

    def _settle_undo(self, step: StepRecord, job: MaintenanceJob) -> None:
        """Arm the step's undo record on agreement, or mark it unavailable, from the job's stored
        result and evidence. Repeating it after a crash changes nothing."""
        run = self.runs.get(step.run_id)
        if run.profile == "undo" and job.result is not None:
            self.operation_key(step)
            record = self.undo.get(run.tenant_id, step.resolved_args["undo_id"])
            assert record is not None
            if record.state == "requested" and job.result["outcome"] == "agreement":
                with contextlib.suppress(UndoConflictError):
                    self.undo.replace(
                        UndoRecord(
                            **{
                                **record.model_dump(),
                                "state": "used",
                                "revision": record.revision + 1,
                            }
                        ),
                        expected_revision=record.revision,
                    )
            return
        if (step.tool, step.tool_version) not in self.undoable or job.result is None:
            return
        undo_id = undo_identity(job.tenant_id, step.run_id, step.step_id, job.action_hash or "")
        record = self.undo.get(job.tenant_id, undo_id)
        if record is None:
            return
        changed = settled(record, job.result["outcome"], job.evidence.get("facts", {}))
        if changed is not None:
            with contextlib.suppress(UndoConflictError):  # another driver settled it first
                self.undo.replace(changed, expected_revision=record.revision)

    def change(self, job: MaintenanceJob, **updates: Any) -> MaintenanceJob:
        changed = MaintenanceJob(**{**job.model_dump(), **updates, "revision": job.revision + 1})
        return self.jobs.replace(changed, expected_revision=job.revision)

    def renew_dispatch(self, step: StepRecord, job: MaintenanceJob) -> MaintenanceJob:
        """Fence every new invocation against takeover and current execution eligibility."""
        current = self.find(step)
        now = self.clock()
        run = self.runs.get(step.run_id)
        active = self.steps.get(step.run_id, step.step_id)
        if (
            current is None
            or current.revision != job.revision
            or current.lease_owner != job.lease_owner
            or current.lease_until is None
            or current.lease_until <= now
            or current.result is not None
            or current.continued
            or now >= current.deadline
            or run.status is not RunStatus.RUNNING
            or active.status is not StepStatus.EXECUTING
            or active.action_hash != job.action_hash
        ):
            raise JobConflictError("dispatch ownership or eligibility changed")
        self._validate_identity(active, current)
        return self.change(current, lease_until=now + JOB_LEASE)

    def refuse(
        self,
        step: StepRecord,
        job: MaintenanceJob,
        code: str,
        observations: tuple[RecordObservation, ...],
    ) -> MaintenanceJob:
        self._validate_identity(step, job)
        if code not in REFUSAL_CODES:
            raise ValueError("not a revision refusal")
        observer = self.refusal_observers.get((step.tool, step.tool_version))
        safe: tuple[RecordObservation, ...] = ()
        if observer is not None:
            # Never persist foreign or undeclared data; a later read stays labeled later.
            with contextlib.suppress(Exception):
                safe = observer.guard(step.resolved_args, observations)
        return self.change(
            job,
            evidence={
                "refusal": {
                    "code": code,
                    "observations": [o.model_dump(mode="json") for o in safe],
                }
            },
            evidence_seq=None,
        )

    def _finish_refusal(self, step: StepRecord, job: MaintenanceJob) -> MaintenanceJob:
        proof = job.evidence["refusal"]
        code = proof["code"]
        evidence = dict(job.evidence)
        observer = self.refusal_observers.get((step.tool, step.tool_version))
        if observer is not None:
            try:
                guard = tuple(RecordObservation.model_validate(o) for o in proof["observations"])
                diagnostic = observer.diagnostic(
                    step.resolved_args,
                    self.runs.get(step.run_id),
                    job.action_hash or "",
                    code,
                    guard,
                    self.clock,
                )
                evidence["diagnostic"] = diagnostic.model_dump(mode="json")
            except Exception:
                evidence["diagnostic_unavailable"] = True
        return self.change(
            job,
            evidence=evidence,
            result={"outcome": "not_dispatched", "code": code},
            due_at=self.clock(),
            lease_owner=None,
            lease_until=None,
        )

    def resolve(self, step: StepRecord, *, owned: MaintenanceJob | None = None) -> StepStatus:
        job = self.find(step)
        if job is None:
            raise JobConflictError("dispatch intent missing; cannot invent a new deadline")
        self._validate_identity(step, job)
        if job.result is None:
            now = self.clock()
            if owned is None or owned.lease_owner != job.lease_owner:
                if job.lease_until is not None and job.lease_until > now:
                    raise JobConflictError("verification lease held")
                job = self.change(job, lease_owner=uuid4().hex, lease_until=now + JOB_LEASE)
            if job.evidence.get("refusal", {}).get("code") in REFUSAL_CODES:
                job = self._finish_refusal(step, job)
                return self._apply(
                    step, job, StepStatus.FAILED, job.result["code"] if job.result else None
                )
            if now >= job.deadline:
                result = ReadbackResult("unavailable", code="verification_deadline")
            else:
                try:
                    result = self.adapters[(step.tool, step.tool_version)](
                        job.operation_key,
                        step.resolved_args,
                        now,
                    )
                except Exception:  # failed reads provide no evidence of non-execution
                    result = ReadbackResult("unavailable", code="readback_unavailable")
            # Deadline is checked after IO as well; a late agreement cannot revive an unknown.
            expired = self.clock() >= job.deadline
            evidence = {
                "expected": dict(result.expected),
                "observed": dict(result.observed),
                "code": result.code,
                "observed_at": self.clock().isoformat(),
            }
            if result.facts:
                evidence["facts"] = dict(result.facts)
            if result.status == "unavailable" and not expired:
                job = self.change(
                    job,
                    evidence=evidence,
                    evidence_seq=None,
                    due_at=min(self.clock() + timedelta(seconds=30), job.deadline),
                    lease_owner=None,
                    lease_until=None,
                )
                return self._apply(step, job, StepStatus.AWAITING_READBACK, None)
            outcome: str = "unknown" if expired else result.status
            code = "verification_deadline" if expired else result.code
            if outcome == "agreement":
                manifest = self.registry.get(step.tool, step.tool_version)
                output = self.executions.load_output(step.run_id, step.step_id)
                if output is None and result.output is not None:
                    # A lost reply is recovered from the target, never by repeating the write.
                    self.executions.save_output(step.run_id, step.step_id, result.output)
                    output = dict(result.output)
                if (
                    job.protocol_error
                    or output is None
                    or any(Draft202012Validator(manifest.output_schema).iter_errors(output))
                ):
                    outcome, code = "protocol_failure", "output_schema_invalid"
                elif result.output is not None and any(
                    output.get(k) != v for k, v in result.output.items()
                ):
                    # A returned id cannot become a reference to an unrelated resource.
                    outcome, code = "protocol_failure", "output_record_mismatch"
            job = self.change(
                job,
                evidence=evidence,
                evidence_seq=None,
                result={"outcome": outcome, "code": code},
                due_at=self.clock(),
                lease_owner=None,
                lease_until=None,
            )
        assert job.result is not None
        status = _RESULT_STATES[job.result["outcome"]]
        return self._apply(step, job, status, job.result["code"])

    def _validate_identity(self, step: StepRecord, job: MaintenanceJob) -> None:
        recomputed = action_hash(
            tool=step.tool,
            version=step.tool_version,
            resolved_args=step.resolved_args,
            run_id=step.run_id,
            step_id=step.step_id,
        )
        run = self.runs.get(step.run_id)
        if (
            job.action_hash != recomputed
            or step.action_hash != recomputed
            or job.operation_key != self.operation_key(step)
            or job.plan_version != run.plan_version
            or job.tenant_id != run.tenant_id
            or job.payload != {"tool": step.tool, "version": step.tool_version}
        ):
            raise JobConflictError("verification identity changed")

    def _apply(
        self,
        step: StepRecord,
        job: MaintenanceJob,
        target: StepStatus,
        code: str | None,
    ) -> StepStatus:
        if job.result is not None:
            self._settle_undo(step, job)
            for attempt in self.executions.attempts(step.run_id, step.step_id):
                if attempt.idempotency_key == job.operation_key and attempt.status == "started":
                    output = self.executions.load_output(step.run_id, step.step_id)
                    succeeded = target is StepStatus.SUCCEEDED and output is not None
                    with contextlib.suppress(AttemptConflictError):
                        self.executions.finish(
                            step.run_id,
                            step.step_id,
                            attempt=attempt.attempt,
                            status="succeeded" if succeeded else "failed",
                            now=self.clock(),
                            output_hash=sha256_hex(canonical_json(output)) if succeeded else None,
                            error=None if succeeded else code,
                        )
        current = self.steps.get(step.run_id, step.step_id)
        if current.action_hash != job.action_hash:
            raise JobConflictError("continuation refers to another action")
        if current.status is StepStatus.EXECUTING and target is not StepStatus.READY:
            current = self.steps.transition(
                current.run_id,
                current.step_id,
                expected=current.status,
                to=StepStatus.VERIFYING,
                expected_action_hash=job.action_hash,
            )
        if current.status not in STEP_TERMINAL and current.status != target:
            updates: dict[str, Any] = {
                "error": code if target is not StepStatus.SUCCEEDED else None
            }
            if target is StepStatus.READY:
                updates.update(
                    preparation={**current.preparation, "reapproval_required": True},
                    policy_decision=None,
                )
            self.steps.transition(
                current.run_id,
                current.step_id,
                expected=current.status,
                to=target,
                expected_action_hash=job.action_hash,
                **updates,
            )
        elif current.status in STEP_TERMINAL and current.status != target:
            raise StaleStateError("a terminal step cannot be rewritten")
        if job.evidence_seq is not None:
            return target
        event = self.audit.record(
            step.run_id,
            "verification.readback",
            "gawe.verifier",
            {
                "step_id": step.step_id,
                "operation_key": job.operation_key,
                "evidence_hash": sha256_hex(canonical_json(job.evidence)),
                "status": target.value,
                "code": code,
            },
            once=True,
        )
        self.change(job, evidence_seq=event.seq)
        return target

    def acknowledge(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        for job in self.jobs.for_run(run.tenant_id, run_id):
            if (
                job.kind == READBACK_KIND
                and job.result is not None
                and not job.continued
                and job.evidence_seq is not None
                and job.plan_version == run.plan_version
            ):
                step = self.steps.get(run_id, run_target(job).step_id)
                expected = _RESULT_STATES[job.result["outcome"]]
                refreshed = expected is StepStatus.READY and step.action_hash != job.action_hash
                if not refreshed and (
                    step.action_hash != job.action_hash or step.status != expected
                ):
                    continue
                with contextlib.suppress(JobConflictError):
                    self.change(job, continued=True)
