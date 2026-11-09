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

Side effects are idempotent end to end: every attempt of a step carries the same idempotency
key (derived from the action hash), and connectors must deduplicate on it. If a worker dies after
the tool returned but before its output was saved, the next worker repeats the call with the same
key; if the output was saved, the tool is not called again. A worker that died mid-attempt is
taken over once its lease expires.

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
from gawe.core.models import PolicyDecision, ToolManifest
from gawe.core.policy.signing import PolicySigner, PolicySigningError
from gawe.core.records import (
    AttemptConflictError,
    ExecutionStore,
    StaleStateError,
    StepRecord,
    StepStore,
)
from gawe.core.registry import RegistryError, ToolRegistry
from gawe.core.statemachine import StepStatus
from gawe.core.verifier import Verifier

ACTOR = "gawe.executor"
_IDEMPOTENCY_DOMAIN = "gawe.idempotency.v1"


class ExecutionRefusedError(Exception):
    """The executor refused to run the step; no side effect happened."""


class ConcurrentExecutionError(ExecutionRefusedError):
    """Another worker owns this step right now."""


class ToolTransientError(Exception):
    """Raised by a tool for failures worth retrying (timeouts, 5xx)."""


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
        retry: RetryPolicy | None = None,
        lease: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._steps = steps
        self._executions = executions
        self._registry = registry
        self._tools = dict(tools)
        self._signer = signer
        self._approvals = approvals
        self._audit = audit
        self._verifier = verifier
        self._retry = retry or RetryPolicy()
        self._lease = lease
        self._clock = clock
        self._sleep = sleep

    # -- public ---------------------------------------------------------------------------

    def execute(self, run_id: str, step_id: str, decision: PolicyDecision) -> StepOutcome:
        """Run one step that policy (and, if required, a human) has cleared."""
        step = self._steps.get(run_id, step_id)
        recomputed = self._recompute_hash(step)
        self._check_authorization(step, decision, recomputed)
        manifest = self._load_manifest(step)  # raises before any state change

        if step.status is StepStatus.VERIFYING:
            return self._resume_verification(step, manifest)
        resuming = step.status is StepStatus.EXECUTING
        if resuming:
            self._take_over_if_stale(step)
        else:
            self._claim(step, decision)

        impl = self._tools.get(step.tool)
        if impl is None:
            return self._fail(step, "tool_not_implemented")
        if any(Draft202012Validator(manifest.input_schema).iter_errors(step.resolved_args)):
            return self._fail(step, "args_schema_invalid")

        output, attempts, error = self._run_attempts(step, impl)
        if output is None:
            return self._fail(step, error or "unknown", attempts=attempts)
        return self._verify(step, manifest, output, attempts)

    # -- authorization --------------------------------------------------------------------

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
        self, step: StepRecord, decision: PolicyDecision, recomputed: str
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
            resuming = step.status is StepStatus.EXECUTING
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
                step.run_id, step.step_id, expected=expected, to=StepStatus.EXECUTING
            )
        except StaleStateError:
            raise ConcurrentExecutionError("another worker claimed this step") from None
        self._audit.record(
            step.run_id,
            "execution.started",
            ACTOR,
            {"step_id": step.step_id, "action_hash": step.action_hash},
        )

    def _take_over_if_stale(self, step: StepRecord) -> None:
        """Resume a step another worker left in `executing`, but only once it has gone quiet.

        Any recorded activity (an attempt starting or finishing) within the lease means a worker
        may still be alive, so we back off. After the lease, an attempt still marked `started`
        is closed as failed so the next attempt can begin.
        """
        attempts = self._executions.attempts(step.run_id, step.step_id)
        last = attempts[-1] if attempts else None
        if last is None:
            return  # claimed but never started an attempt; racing starters collide on attempt 1
        last_activity = last.finished_at or last.started_at
        if self._clock() < last_activity + self._lease:
            raise ConcurrentExecutionError("another worker is active on this step")
        if last.status != "started":
            return
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

    def _move(self, step: StepRecord, expected: StepStatus, to: StepStatus, **updates: Any) -> None:
        """A status change that reports losing a race as ConcurrentExecutionError."""
        try:
            self._steps.transition(step.run_id, step.step_id, expected=expected, to=to, **updates)
        except StaleStateError:
            raise ConcurrentExecutionError("the step changed under this worker") from None

    # -- running --------------------------------------------------------------------------

    def _run_attempts(
        self, step: StepRecord, impl: ToolImplementation
    ) -> tuple[Mapping[str, Any] | None, int, str | None]:
        assert step.action_hash is not None
        key = idempotency_key(step.action_hash)

        saved = self._executions.load_output(step.run_id, step.step_id)
        history = self._executions.attempts(step.run_id, step.step_id)
        if saved is not None:  # the tool already returned; never call it again
            return saved, len(history), None

        error: str | None = None
        while True:
            used = len(self._executions.attempts(step.run_id, step.step_id))
            if used >= self._retry.max_attempts:
                return None, used, error or "retries_exhausted"
            if used > 0:
                self._sleep(self._retry.delay(used))
            attempt = used + 1
            try:
                self._executions.start(
                    step.run_id, step.step_id, attempt=attempt, idempotency_key=key,
                    now=self._clock(),
                )  # fmt: skip
            except AttemptConflictError:
                raise ConcurrentExecutionError("another worker started an attempt") from None

            call = ToolCall(step.run_id, step.step_id, attempt, key)
            try:
                output = dict(impl(step.resolved_args, call))
                output_hash = sha256_hex(canonical_json(output))
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
