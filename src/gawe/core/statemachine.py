"""Run and step state machines.

These tables are the only definition of which status changes are legal. Stores call
`check_*_transition` before writing, so no code path can, for example, move a step from
`awaiting_approval` straight to `executing`.

Status values match the CHECK constraints in migrations/sql/0001_init.sql; a unit test fails if
they drift apart.
"""

from enum import StrEnum
from typing import Final


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED_FOR_APPROVAL = "paused_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DENIED = "denied"


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    POLICY_CHECKED = "policy_checked"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    DENIED = "denied"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IllegalTransitionError(Exception):
    """The requested status change is not in the transition table."""


_R = RunStatus
_S = StepStatus

RUN_TRANSITIONS: Final[dict[RunStatus, frozenset[RunStatus]]] = {
    _R.CREATED: frozenset({_R.PLANNING, _R.CANCELLED, _R.FAILED}),
    _R.PLANNING: frozenset({_R.RUNNING, _R.CANCELLED, _R.FAILED}),
    # running -> planning is a replan after a failed step; it is a new plan version that goes
    # through the same validation and policy path as the first one.
    _R.RUNNING: frozenset(
        {_R.PLANNING, _R.PAUSED_FOR_APPROVAL, _R.COMPLETED, _R.FAILED, _R.CANCELLED, _R.DENIED}
    ),
    _R.PAUSED_FOR_APPROVAL: frozenset({_R.RUNNING, _R.CANCELLED, _R.FAILED, _R.DENIED}),
    _R.COMPLETED: frozenset(),
    _R.FAILED: frozenset(),
    _R.CANCELLED: frozenset(),
    _R.DENIED: frozenset(),
}

STEP_TRANSITIONS: Final[dict[StepStatus, frozenset[StepStatus]]] = {
    _S.PENDING: frozenset({_S.READY, _S.FAILED}),
    _S.READY: frozenset({_S.POLICY_CHECKED, _S.FAILED}),
    _S.POLICY_CHECKED: frozenset({_S.AWAITING_APPROVAL, _S.EXECUTING, _S.DENIED, _S.FAILED}),
    # An expired approval fails the step; getting a fresh approval means replanning.
    _S.AWAITING_APPROVAL: frozenset({_S.APPROVED, _S.REJECTED, _S.FAILED}),
    _S.APPROVED: frozenset({_S.EXECUTING, _S.FAILED}),
    # Retries happen inside `executing` (tracked per attempt in the executions table).
    _S.EXECUTING: frozenset({_S.VERIFYING, _S.FAILED}),
    _S.VERIFYING: frozenset({_S.SUCCEEDED, _S.FAILED}),
    _S.REJECTED: frozenset(),
    _S.DENIED: frozenset(),
    _S.SUCCEEDED: frozenset(),
    _S.FAILED: frozenset(),
}

RUN_TERMINAL: Final = frozenset(s for s, targets in RUN_TRANSITIONS.items() if not targets)
STEP_TERMINAL: Final = frozenset(s for s, targets in STEP_TRANSITIONS.items() if not targets)


def check_run_transition(current: RunStatus, new: RunStatus) -> None:
    if new not in RUN_TRANSITIONS[current]:
        raise IllegalTransitionError(f"run cannot move from {current.value} to {new.value}")


def check_step_transition(current: StepStatus, new: StepStatus) -> None:
    if new not in STEP_TRANSITIONS[current]:
        raise IllegalTransitionError(f"step cannot move from {current.value} to {new.value}")
