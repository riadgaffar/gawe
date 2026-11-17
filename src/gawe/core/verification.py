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
from gawe.core.records import (
    AttemptConflictError,
    ExecutionStore,
    StaleStateError,
    StepRecord,
    StepStore,
)
from gawe.core.registry import ToolRegistry
from gawe.core.runs import RunStore
from gawe.core.statemachine import STEP_TERMINAL, RunStatus, StepStatus

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
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("verification window must be positive")
        self.window = window
        # (tool, version) writes that grant time-bound access: each gets an expiry job, added
        # before its dispatch intent.
        self.expiring = expiring
        self.jobs, self.steps, self.executions, self.runs = jobs, steps, executions, runs
        self.registry, self.adapters, self.audit, self.clock = (
            registry,
            dict(adapters),
            audit,
            clock,
        )

    def find(self, step: StepRecord) -> MaintenanceJob | None:
        run = self.runs.get(step.run_id)
        assert step.action_hash is not None
        key = operation_identity(step.action_hash, run.plan_version)
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
        old = self.find(step)
        if old:
            return old, False
        run = self.runs.get(step.run_id)
        assert step.action_hash is not None
        now = self.clock()
        key = operation_identity(step.action_hash, run.plan_version)
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
            or job.operation_key != operation_identity(recomputed, run.plan_version)
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
