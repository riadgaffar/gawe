"""Step and execution records, with the store interfaces the executor depends on.

The in-memory stores are the reference implementation. They enforce the same rules as the
Postgres schema (hash required once a step is past policy, unique attempt numbers, a started
attempt has no finish time), and every status change is a compare-and-set, which is what makes
"exactly one worker executes a step" hold. A Postgres store will do the same with
`UPDATE ... WHERE status = :expected`.
"""

import threading
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from gawe.core.models import FrozenModel
from gawe.core.statemachine import StepStatus, check_step_transition

SHA256_PATTERN = r"^[0-9a-f]{64}$"
_HASH_OPTIONAL = frozenset({StepStatus.PENDING, StepStatus.READY, StepStatus.FAILED})
_UPDATABLE = frozenset({"action_hash", "error", "resolved_args"})


class StepNotFoundError(KeyError):
    pass


class StaleStateError(Exception):
    """The step's status is not what the caller expected: someone else changed it first."""


class AttemptConflictError(Exception):
    """An execution attempt was started or finished concurrently by another worker."""


class StepRecord(FrozenModel):
    run_id: str
    step_id: str
    tool: str
    tool_version: str
    args_template: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    resolved_args: dict[str, Any] = Field(default_factory=dict)
    action_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    status: StepStatus = StepStatus.PENDING
    error: str | None = None

    @model_validator(mode="after")
    def _hash_required_past_policy(self) -> "StepRecord":
        if self.status not in _HASH_OPTIONAL and self.action_hash is None:
            raise ValueError(f"a step in status {self.status.value} must carry its action_hash")
        return self


ExecutionStatus = Literal["started", "succeeded", "failed"]


class ExecutionRecord(FrozenModel):
    run_id: str
    step_id: str
    attempt: int = Field(ge=1)
    idempotency_key: str
    status: ExecutionStatus
    error: str | None = None
    output_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    started_at: datetime
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _finished_iff_not_started(self) -> "ExecutionRecord":
        if (self.status == "started") != (self.finished_at is None):
            raise ValueError("finished_at is set exactly when the attempt is no longer 'started'")
        return self


class StepStore(Protocol):
    def add(self, step: StepRecord) -> None: ...

    def get(self, run_id: str, step_id: str) -> StepRecord: ...

    def transition(
        self, run_id: str, step_id: str, *, expected: StepStatus, to: StepStatus, **updates: Any
    ) -> StepRecord:
        """Atomically move a step from `expected` to `to` (raises StaleStateError otherwise)."""
        ...


class ExecutionStore(Protocol):
    def attempts(self, run_id: str, step_id: str) -> list[ExecutionRecord]: ...

    def start(
        self, run_id: str, step_id: str, *, attempt: int, idempotency_key: str, now: datetime
    ) -> ExecutionRecord:
        """Begin attempt number `attempt`; AttemptConflictError unless it is the next number."""
        ...

    def finish(
        self,
        run_id: str,
        step_id: str,
        *,
        attempt: int,
        status: Literal["succeeded", "failed"],
        now: datetime,
        error: str | None = None,
        output_hash: str | None = None,
    ) -> ExecutionRecord:
        """Close a started attempt; AttemptConflictError if it is not currently started."""
        ...

    def save_output(self, run_id: str, step_id: str, output: Mapping[str, Any]) -> None:
        """Store the tool's output. The first write wins; later writes are ignored."""
        ...

    def load_output(self, run_id: str, step_id: str) -> dict[str, Any] | None: ...


class InMemoryStepStore:
    def __init__(self) -> None:
        self._steps: dict[tuple[str, str], StepRecord] = {}
        self._lock = threading.Lock()

    def add(self, step: StepRecord) -> None:
        key = (step.run_id, step.step_id)
        with self._lock:
            if key in self._steps:
                raise ValueError(f"step {step.step_id} already exists in run {step.run_id}")
            self._steps[key] = step

    def get(self, run_id: str, step_id: str) -> StepRecord:
        with self._lock:
            try:
                return self._steps[(run_id, step_id)]
            except KeyError:
                raise StepNotFoundError(f"no step {step_id} in run {run_id}") from None

    def transition(
        self, run_id: str, step_id: str, *, expected: StepStatus, to: StepStatus, **updates: Any
    ) -> StepRecord:
        unknown = set(updates) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update fields: {sorted(unknown)}")
        check_step_transition(expected, to)
        with self._lock:
            try:
                current = self._steps[(run_id, step_id)]
            except KeyError:
                raise StepNotFoundError(f"no step {step_id} in run {run_id}") from None
            if current.status is not expected:
                raise StaleStateError(
                    f"step {step_id} is {current.status.value}, expected {expected.value}"
                )
            changed = StepRecord(**{**current.model_dump(), **updates, "status": to})
            self._steps[(run_id, step_id)] = changed
            return changed


class InMemoryExecutionStore:
    def __init__(self) -> None:
        self._attempts: dict[tuple[str, str], list[ExecutionRecord]] = {}
        self._outputs: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def attempts(self, run_id: str, step_id: str) -> list[ExecutionRecord]:
        with self._lock:
            return list(self._attempts.get((run_id, step_id), []))

    def start(
        self, run_id: str, step_id: str, *, attempt: int, idempotency_key: str, now: datetime
    ) -> ExecutionRecord:
        with self._lock:
            history = self._attempts.setdefault((run_id, step_id), [])
            if attempt != len(history) + 1:
                raise AttemptConflictError(f"attempt {attempt} is not next (have {len(history)})")
            record = ExecutionRecord(
                run_id=run_id,
                step_id=step_id,
                attempt=attempt,
                idempotency_key=idempotency_key,
                status="started",
                started_at=now,
            )
            history.append(record)
            return record

    def finish(
        self,
        run_id: str,
        step_id: str,
        *,
        attempt: int,
        status: Literal["succeeded", "failed"],
        now: datetime,
        error: str | None = None,
        output_hash: str | None = None,
    ) -> ExecutionRecord:
        with self._lock:
            history = self._attempts.get((run_id, step_id), [])
            if attempt < 1 or attempt > len(history):
                raise AttemptConflictError(f"no attempt {attempt}")
            current = history[attempt - 1]
            if current.status != "started":
                raise AttemptConflictError(f"attempt {attempt} is already {current.status}")
            closed = ExecutionRecord(
                **{
                    **current.model_dump(),
                    "status": status,
                    "error": error,
                    "output_hash": output_hash,
                    "finished_at": now,
                }
            )
            history[attempt - 1] = closed
            return closed

    def save_output(self, run_id: str, step_id: str, output: Mapping[str, Any]) -> None:
        with self._lock:
            self._outputs.setdefault((run_id, step_id), dict(output))

    def load_output(self, run_id: str, step_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._outputs.get((run_id, step_id))
            return None if found is None else dict(found)
