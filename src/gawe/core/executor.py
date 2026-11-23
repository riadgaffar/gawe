"""The only code path that runs a tool.

Before any side effect the executor re-derives everything it relies on instead of trusting
earlier stages:

1. the stored `action_hash` must equal the hash recomputed from the stored tool, version,
   resolved args, run and step (catches tampering or drift after approval);
2. the policy decision must carry a valid signature *for that hash* (forged or replayed
   decisions fail), and must not be a deny;
3. `needs_approval` actions need an approval for that exact hash that has not expired;
4. the tool must be registered at the stored version and its args must satisfy its schema;
5. the step must be won by compare-and-set into `executing`, so concurrent workers run it once.

Before all of these, a step whose tool version is in the executor's prepare-only setting is
refused outright (M20). The setting is the one the executor runs with now, never anything stored
on the step: no decision, approval, retry or recovery can let such an action dispatch.

Shipped writes persist an operation intent and fixed deadline before invocation. Their key binds
both action hash and plan version. Only a connector-declared pre-dispatch rejection may retry;
a possible acceptance or saved reply routes exclusively to independent system read-back. Recovery
never repeats such a write. Generic read-tool attempts retain the ordinary lease/retry path.

Audit events contain hashes and short codes only, never arguments or tool output.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, Protocol

from jsonschema import Draft202012Validator

from gawe.core.approvals import ApprovalNotGrantedError, ApprovalService
from gawe.core.audit import AuditLog
from gawe.core.canonical import action_hash, canonical_json, sha256_hex
from gawe.core.maintenance import JobConflictError, MaintenanceJob
from gawe.core.models import PolicyDecision, ToolManifest
from gawe.core.policy.signing import PolicySigner, PolicySigningError
from gawe.core.preparation import RecipientChanged
from gawe.core.readback import operation_identity
from gawe.core.records import (
    AttemptConflictError,
    ExecutionStore,
    StaleStateError,
    StepRecord,
    StepStore,
)
from gawe.core.registry import RegistryError, ToolRegistry
from gawe.core.statemachine import StepStatus
from gawe.core.verification import VerificationResolver
from gawe.core.verifier import Verifier

ACTOR = "gawe.executor"
_IDEMPOTENCY_DOMAIN = "gawe.idempotency.v1"


class ExecutionRefusedError(Exception):
    """The executor refused to run the step; no side effect happened."""


class ConcurrentExecutionError(ExecutionRefusedError):
    """Another worker owns this step right now."""


class ToolTransientError(Exception):
    """Raised by a tool for failures worth retrying (timeouts, 5xx)."""


class ToolPreDispatchError(Exception):
    """Connector guarantees this invocation failed before any possible external acceptance."""


@dataclass(frozen=True)
class ToolCall:
    run_id: str
    step_id: str
    attempt: int
    idempotency_key: str


class ToolImplementation(Protocol):
    def __call__(self, args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

    def delay(self, failed_attempts: int) -> float:
        return float(min(self.max_delay, self.base_delay * 2 ** (failed_attempts - 1)))


@dataclass(frozen=True)
class StepOutcome:
    step_id: str
    status: StepStatus
    attempts: int
    output_hash: str | None = None
    error: str | None = None


def idempotency_key(step_action_hash: str) -> str:
    return sha256_hex(
        canonical_json({"domain": _IDEMPOTENCY_DOMAIN, "action_hash": step_action_hash})
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _error_code(exc: BaseException) -> str:
    """A safe label for an error: a tool-declared `.code` string, else the class name."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code.isidentifier() else type(exc).__name__


class Executor:
    def __init__(
        self,
        *,
        steps: StepStore,
        executions: ExecutionStore,
        registry: ToolRegistry,
        tools: Mapping[str, ToolImplementation],
        signer: PolicySigner,
        approvals: ApprovalService,
        audit: AuditLog,
        verifier: Verifier,
        verification: VerificationResolver | None = None,
        retry: RetryPolicy | None = None,
        lease: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        prepare_only: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self._prepare_only = prepare_only
        self._steps = steps
        self._executions = executions
        self._registry = registry
        self._tools = dict(tools)
        self._signer = signer
        self._approvals = approvals
        self._audit = audit
        self._verifier = verifier
        self.verification = verification
        self._retry = retry or RetryPolicy()
        self._lease = lease
        self._clock = clock
        self._sleep = sleep

    # -- public ---------------------------------------------------------------------------

    def execute(self, run_id: str, step_id: str, decision: PolicyDecision) -> StepOutcome:
        """Run one step that policy (and, if required, a human) has cleared."""
        step = self._steps.get(run_id, step_id)
        recomputed = self._recompute_hash(step)
        if self.verification and (step.tool, step.tool_version) in self.verification.adapters:
            existing = self.verification.find(step)
            if existing is not None and not existing.continued:
                # This branch can only read/apply a persisted intent, never dispatch a tool.
                return self.reconcile(step)
        if (step.tool, step.tool_version) in self._prepare_only:
            # M20: whatever was decided or approved, this action is a person's to carry out.
            self._refuse(step, "prepare_only")
        self._check_authorization(step, decision, recomputed)
        manifest = self._load_manifest(step)  # raises before any state change
        if manifest.retired:
            self._refuse(step, "unsupported_tool_version")
        if (
            self.verification
            and (step.tool, step.tool_version) in self.verification.adapters
            and step.status in {StepStatus.VERIFYING, StepStatus.AWAITING_READBACK}
        ):
            self._refuse(step, "legacy_write_reconciliation_required")

        if step.status is StepStatus.VERIFYING:
            return self._resume_verification(step, manifest)
        # The attempts this worker has vetted. Any attempt beyond them belongs to another worker.
        resuming = step.status is StepStatus.EXECUTING
        if resuming:
            vetted = self._take_over_if_stale(step)
        else:
            vetted = len(self._executions.attempts(step.run_id, step.step_id))
            self._claim(step, decision)

        impl = self._tools.get(step.tool)
        if impl is None:
            return self._fail(step, "tool_not_implemented")
        if any(Draft202012Validator(manifest.input_schema).iter_errors(step.resolved_args)):
            return self._fail(step, "args_schema_invalid")

        if self.verification and (step.tool, step.tool_version) in self.verification.adapters:
            return self._execute_write(step, manifest, impl, decision)

        output, attempts, error = self._run_attempts(step, impl, vetted)
        if output is None:
            if error == "recipient_changed" and manifest.preparation_required:
                self._move(
                    step,
                    StepStatus.EXECUTING,
                    StepStatus.READY,
                    preparation={**step.preparation, "reapproval_required": True},
                    policy_decision=None,
                )
                return StepOutcome(step.step_id, StepStatus.READY, attempts, error=error)
            return self._fail(step, error or "unknown", attempts=attempts)
        return self._verify(step, manifest, output, attempts)

    # -- authorization --------------------------------------------------------------------

    def reconcile(self, step: StepRecord, *, owned: MaintenanceJob | None = None) -> StepOutcome:
        """Read a dispatched effect without checking an expired approval or entering write code."""
        assert self.verification is not None
        try:
            status = self.verification.resolve(step, owned=owned)
        except (JobConflictError, StaleStateError) as exc:
            raise ConcurrentExecutionError(str(exc)) from exc
        attempts = len(self._executions.attempts(step.run_id, step.step_id))
        saved = self._steps.get(step.run_id, step.step_id)
        output = self._executions.load_output(step.run_id, step.step_id)
        output_hash = (
            sha256_hex(canonical_json(output))
            if status is StepStatus.SUCCEEDED and output is not None
            else None
        )
        return StepOutcome(
            step.step_id, status, attempts, output_hash=output_hash, error=saved.error
        )

    def _execute_write(
        self,
        step: StepRecord,
        manifest: ToolManifest,
        impl: ToolImplementation,
        decision: PolicyDecision,
    ) -> StepOutcome:
        assert self.verification is not None
        history = self._executions.attempts(step.run_id, step.step_id)
        assert step.action_hash is not None
        plan_version = self.verification.runs.get(step.run_id).plan_version
        keys = {
            idempotency_key(step.action_hash),
            operation_identity(step.action_hash, plan_version),
        }
        if any(a.idempotency_key in keys for a in history):
            # A pre-S3 executing row has no trustworthy original verification deadline.
            self._refuse(step, "legacy_write_reconciliation_required")
        try:
            job, inserted = self.verification.prepare(step)
        except JobConflictError as exc:
            raise ConcurrentExecutionError(str(exc)) from exc
        if not inserted:
            return self.reconcile(step)
        attempts = 0
        while attempts < self._retry.max_attempts:
            # Each genuinely new invocation still needs live authorization. Readback does not.
            try:
                self._check_authorization(step, decision, step.action_hash, fresh_dispatch=True)
            except ExecutionRefusedError:
                job = self.verification.change(
                    job,
                    result={
                        "outcome": "not_dispatched",
                        "code": "approval_missing",
                    },
                )
                return self.reconcile(step, owned=job)
            # An expired or superseded dispatcher can only reconcile, never retry a write.
            try:
                job = self.verification.renew_dispatch(step, job)
            except JobConflictError:
                return self.reconcile(step)
            if self._clock() >= job.deadline:
                return self.reconcile(step, owned=job)
            attempts += 1
            number = len(history) + attempts
            try:
                self._executions.start(
                    step.run_id,
                    step.step_id,
                    attempt=number,
                    idempotency_key=job.operation_key,
                    now=self._clock(),
                )
            except AttemptConflictError as exc:
                raise ConcurrentExecutionError("attempt already started") from exc
            # Starting an attempt is a separate store call and may have stalled past the lease.
            try:
                job = self.verification.renew_dispatch(step, job)
            except JobConflictError:
                return self.reconcile(step)
            try:
                reply = impl(
                    step.resolved_args,
                    ToolCall(
                        step.run_id,
                        step.step_id,
                        number,
                        job.operation_key,
                    ),
                )
            except RecipientChanged:
                # Commit the no-dispatch proof first. Recovery can close an unfinished attempt
                # from this result, but must not infer non-dispatch from a missing target record.
                job = self.verification.change(
                    job,
                    result={
                        "outcome": "recipient_changed",
                        "code": "recipient_changed",
                    },
                )
                self._close_failed(step, number, "recipient_changed", retryable=False)
                return self.reconcile(step, owned=job)
            except ToolPreDispatchError as exc:
                retryable = (
                    isinstance(exc, ToolTransientError) and attempts < self._retry.max_attempts
                )
                self._close_failed(step, number, _error_code(exc), retryable=retryable)
                if retryable:
                    self._sleep(self._retry.delay(attempts))
                    continue
                job = self.verification.change(
                    job,
                    result={
                        "outcome": "not_dispatched",
                        "code": _error_code(exc),
                    },
                )
                return self.reconcile(step, owned=job)
            except Exception as exc:
                # Timeout/exception with possible acceptance is NOT a retryable write failure.
                self._close_failed(step, number, _error_code(exc), retryable=False)
                return self.reconcile(step, owned=job)
            try:
                output = dict(reply)
                output_hash = sha256_hex(canonical_json(output))
            except Exception:
                job = self.verification.change(job, protocol_error="output_schema_invalid")
                self._close_failed(step, number, "output_schema_invalid", retryable=False)
                return self.reconcile(step, owned=job)
            self._executions.save_output(step.run_id, step.step_id, output)
            if any(Draft202012Validator(manifest.output_schema).iter_errors(output)):
                job = self.verification.change(job, protocol_error="output_schema_invalid")
            self._executions.finish(
                step.run_id,
                step.step_id,
                attempt=number,
                status="succeeded",
                now=self._clock(),
                output_hash=output_hash,
            )
            self._audit.record(
                step.run_id,
                "execution.succeeded",
                ACTOR,
                {
                    "step_id": step.step_id,
                    "attempt": number,
                    "output_hash": output_hash,
                },
            )
            return self.reconcile(step, owned=job)
        raise AssertionError("bounded dispatch loop must return")

    def _recompute_hash(self, step: StepRecord) -> str:
        recomputed = action_hash(
            tool=step.tool,
            version=step.tool_version,
            resolved_args=step.resolved_args,
            run_id=step.run_id,
            step_id=step.step_id,
        )
        if step.action_hash != recomputed:
            self._refuse(step, "stored_hash_drift")
        return recomputed

    def _check_authorization(
        self,
        step: StepRecord,
        decision: PolicyDecision,
        recomputed: str,
        *,
        fresh_dispatch: bool = False,
    ) -> None:
        if decision.step_id != step.step_id:
            self._refuse(step, "decision_for_other_step")
        try:
            self._signer.require_valid(decision, expected_action_hash=recomputed)
        except PolicySigningError:
            self._refuse(step, "invalid_policy_decision")
        if decision.outcome == "deny":
            self._refuse(step, "policy_denied")
        if decision.outcome == "needs_approval":
            # Resuming an execution already running under a valid approval may outlive its TTL.
            resuming = step.status is StepStatus.EXECUTING and not fresh_dispatch
            try:
                self._approvals.require_approved(
                    run_id=step.run_id,
                    step_id=step.step_id,
                    action_hash=recomputed,
                    enforce_expiry=not resuming,
                )
            except ApprovalNotGrantedError:
                self._refuse(step, "approval_missing")

    def _load_manifest(self, step: StepRecord) -> ToolManifest:
        try:
            return self._registry.get(step.tool, step.tool_version)
        except RegistryError:
            if step.tool == "email.send" and step.tool_version == "1.0.0":
                code = (
                    "legacy_send_reconciliation_required"
                    if step.status in {StepStatus.EXECUTING, StepStatus.VERIFYING}
                    else "unsupported_email_version"
                )
                self._refuse(step, code)
            self._refuse(step, "tool_not_registered")

    def _refuse(self, step: StepRecord, reason: str) -> NoReturn:
        self._audit.record(
            step.run_id,
            "execution.refused",
            ACTOR,
            {"step_id": step.step_id, "reason": reason},
        )
        raise ExecutionRefusedError(reason)

    # -- ownership ------------------------------------------------------------------------

    def _claim(self, step: StepRecord, decision: PolicyDecision) -> None:
        expected = (
            StepStatus.APPROVED
            if decision.outcome == "needs_approval"
            else StepStatus.POLICY_CHECKED
        )
        if step.status is not expected:
            self._refuse(step, f"step_not_{expected.value}")
        try:
            self._steps.transition(
                step.run_id,
                step.step_id,
                expected=expected,
                to=StepStatus.EXECUTING,
                expected_action_hash=step.action_hash,
            )
        except StaleStateError:
            raise ConcurrentExecutionError("another worker claimed this step") from None
        self._audit.record(
            step.run_id,
            "execution.started",
            ACTOR,
            {"step_id": step.step_id, "action_hash": step.action_hash},
        )

    def _take_over_if_stale(self, step: StepRecord) -> int:
        """Resume a step another worker left in `executing`, but only once it has gone quiet.

        Any recorded activity (an attempt starting or finishing) within the lease means a worker
        may still be alive, so we back off. After the lease, an attempt still marked `started`
        is closed as failed so the next attempt can begin. Returns how many attempts were vetted.
        """
        attempts = self._executions.attempts(step.run_id, step.step_id)
        last = attempts[-1] if attempts else None
        if last is None:
            return 0  # claimed but never started an attempt; racing starters collide on attempt 1
        last_activity = last.finished_at or last.started_at
        if self._clock() < last_activity + self._lease:
            raise ConcurrentExecutionError("another worker is active on this step")
        if last.status != "started":
            return len(attempts)
        try:
            self._executions.finish(
                step.run_id,
                step.step_id,
                attempt=last.attempt,
                status="failed",
                now=self._clock(),
                error="lease_expired",
            )
        except AttemptConflictError:
            raise ConcurrentExecutionError("another worker took over first") from None
        self._audit.record(
            step.run_id,
            "execution.resumed",
            ACTOR,
            {"step_id": step.step_id, "abandoned_attempt": last.attempt},
        )
        return len(attempts)

    def _move(self, step: StepRecord, expected: StepStatus, to: StepStatus, **updates: Any) -> None:
        """A status change that reports losing a race as ConcurrentExecutionError."""
        try:
            self._steps.transition(
                step.run_id,
                step.step_id,
                expected=expected,
                to=to,
                expected_action_hash=step.action_hash,
                **updates,
            )
        except StaleStateError:
            raise ConcurrentExecutionError("the step changed under this worker") from None

    # -- running --------------------------------------------------------------------------

    def _run_attempts(
        self, step: StepRecord, impl: ToolImplementation, vetted: int
    ) -> tuple[Mapping[str, Any] | None, int, str | None]:
        """Dispatch attempts numbered after the `vetted` ones this worker took ownership of.

        An attempt this worker neither vetted nor started belongs to a racing worker, finished
        or not, so this worker backs off instead of retrying it.
        """
        assert step.action_hash is not None
        key = idempotency_key(step.action_hash)

        saved = self._executions.load_output(step.run_id, step.step_id)
        history = self._executions.attempts(step.run_id, step.step_id)
        if saved is not None:  # the tool already returned; never call it again
            return saved, len(history), None

        error: str | None = None
        started = 0
        while True:
            all_history = self._executions.attempts(step.run_id, step.step_id)
            if len(all_history) != vetted + started:
                raise ConcurrentExecutionError("another worker started an attempt")
            current_history = [a for a in all_history if a.idempotency_key == key]
            used = len(current_history)
            if current_history and current_history[-1].error == "recipient_changed":
                return None, len(all_history), "recipient_changed"
            if used >= self._retry.max_attempts:
                return None, used, error or "retries_exhausted"
            if used > 0:
                self._sleep(self._retry.delay(used))
            attempt = vetted + started + 1
            try:
                self._executions.start(
                    step.run_id, step.step_id, attempt=attempt, idempotency_key=key,
                    now=self._clock(),
                )  # fmt: skip
            except AttemptConflictError:
                raise ConcurrentExecutionError("another worker started an attempt") from None
            started += 1

            call = ToolCall(step.run_id, step.step_id, attempt, key)
            try:
                output = dict(impl(step.resolved_args, call))
                output_hash = sha256_hex(canonical_json(output))
            except RecipientChanged:
                self._close_failed(step, attempt, "recipient_changed", retryable=False)
                return None, attempt, "recipient_changed"
            except ToolTransientError as exc:
                error = _error_code(exc)
                self._close_failed(step, attempt, error, retryable=True)
                continue
            except Exception as exc:  # noqa: BLE001 - any tool failure is a permanent failure
                error = _error_code(exc)
                self._close_failed(step, attempt, error, retryable=False)
                return None, attempt, error

            self._executions.save_output(step.run_id, step.step_id, output)
            self._executions.finish(
                step.run_id, step.step_id, attempt=attempt, status="succeeded",
                now=self._clock(), output_hash=output_hash,
            )  # fmt: skip
            self._audit.record(
                step.run_id,
                "execution.succeeded",
                ACTOR,
                {"step_id": step.step_id, "attempt": attempt, "output_hash": output_hash},
            )
            return output, attempt, None

    def _close_failed(self, step: StepRecord, attempt: int, code: str, *, retryable: bool) -> None:
        self._executions.finish(
            step.run_id, step.step_id, attempt=attempt, status="failed", now=self._clock(),
            error=code,
        )  # fmt: skip
        self._audit.record(
            step.run_id,
            "execution.attempt_failed",
            ACTOR,
            {"step_id": step.step_id, "attempt": attempt, "error": code, "retryable": retryable},
        )

    # -- verification and failure ---------------------------------------------------------

    def _resume_verification(self, step: StepRecord, manifest: ToolManifest) -> StepOutcome:
        """Finish a step whose worker died after the tool returned but before verification ended."""
        output = self._executions.load_output(step.run_id, step.step_id)
        attempts = len(self._executions.attempts(step.run_id, step.step_id))
        if output is None:
            self._move(step, StepStatus.VERIFYING, StepStatus.FAILED, error="output_missing")
            return StepOutcome(step.step_id, StepStatus.FAILED, attempts, error="output_missing")
        return self._verify(step, manifest, output, attempts, already_verifying=True)

    def _verify(
        self,
        step: StepRecord,
        manifest: ToolManifest,
        output: Mapping[str, Any],
        attempts: int,
        *,
        already_verifying: bool = False,
    ) -> StepOutcome:
        if not already_verifying:
            self._move(step, StepStatus.EXECUTING, StepStatus.VERIFYING)
        result = self._verifier.verify(manifest, step.resolved_args, output)
        output_hash = sha256_hex(canonical_json(output))
        self._audit.record(
            step.run_id,
            "verification.passed" if result.ok else "verification.failed",
            ACTOR,
            {"step_id": step.step_id, "failures": list(result.failures)},
        )
        if not result.ok:
            error = "verification_failed:" + ",".join(result.failures)
            self._move(step, StepStatus.VERIFYING, StepStatus.FAILED, error=error)
            return StepOutcome(step.step_id, StepStatus.FAILED, attempts, output_hash, error)
        self._move(step, StepStatus.VERIFYING, StepStatus.SUCCEEDED)
        return StepOutcome(step.step_id, StepStatus.SUCCEEDED, attempts, output_hash)

    def _fail(self, step: StepRecord, error: str, *, attempts: int = 0) -> StepOutcome:
        self._move(step, StepStatus.EXECUTING, StepStatus.FAILED, error=error)
        self._audit.record(
            step.run_id, "step.failed", ACTOR, {"step_id": step.step_id, "error": error}
        )
        return StepOutcome(step.step_id, StepStatus.FAILED, attempts, error=error)
