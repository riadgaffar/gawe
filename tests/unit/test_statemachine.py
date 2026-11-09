import re
from pathlib import Path

import pytest

from gawe.core.statemachine import (
    RUN_TERMINAL,
    RUN_TRANSITIONS,
    STEP_TERMINAL,
    STEP_TRANSITIONS,
    IllegalTransitionError,
    RunStatus,
    StepStatus,
    check_run_transition,
    check_step_transition,
)

SQL = (Path(__file__).parent.parent.parent / "migrations" / "sql" / "0001_init.sql").read_text()


def check_list(table_marker: str) -> set[str]:
    """Status values in the first `status ... CHECK (status IN (...))` after the table header."""
    section = SQL[SQL.index(f"CREATE TABLE {table_marker}") :]
    match = re.search(r"status\s+text[^,]*?CHECK \(status IN \((.*?)\)\)", section, re.S)
    assert match, table_marker
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def test_enums_match_database_check_constraints() -> None:
    assert {s.value for s in RunStatus} == check_list("runs")
    assert {s.value for s in StepStatus} == check_list("steps")


def test_every_status_has_a_transition_entry() -> None:
    assert set(RUN_TRANSITIONS) == set(RunStatus)
    assert set(STEP_TRANSITIONS) == set(StepStatus)


def test_terminal_states_are_final() -> None:
    assert {
        StepStatus.REJECTED,
        StepStatus.DENIED,
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
    } == STEP_TERMINAL
    assert {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.DENIED,
    } == RUN_TERMINAL


@pytest.mark.parametrize("target", list(StepStatus))
def test_nothing_leaves_a_terminal_step(target: StepStatus) -> None:
    for terminal in STEP_TERMINAL:
        with pytest.raises(IllegalTransitionError):
            check_step_transition(terminal, target)


def test_execution_is_only_reachable_through_policy_or_approval() -> None:
    entering = {s for s, targets in STEP_TRANSITIONS.items() if StepStatus.EXECUTING in targets}
    assert entering == {StepStatus.POLICY_CHECKED, StepStatus.APPROVED}


def test_approval_cannot_be_skipped() -> None:
    for illegal in (StepStatus.EXECUTING, StepStatus.SUCCEEDED, StepStatus.VERIFYING):
        with pytest.raises(IllegalTransitionError):
            check_step_transition(StepStatus.AWAITING_APPROVAL, illegal)
    with pytest.raises(IllegalTransitionError):
        check_step_transition(StepStatus.REJECTED, StepStatus.APPROVED)
    with pytest.raises(IllegalTransitionError):
        check_step_transition(StepStatus.DENIED, StepStatus.EXECUTING)


def test_verification_cannot_be_skipped() -> None:
    with pytest.raises(IllegalTransitionError):
        check_step_transition(StepStatus.EXECUTING, StepStatus.SUCCEEDED)


def test_happy_paths_are_legal() -> None:
    path = ["pending", "ready", "policy_checked", "awaiting_approval", "approved", "executing",
            "verifying", "succeeded"]  # fmt: skip
    for a, b in zip(path, path[1:], strict=False):
        check_step_transition(StepStatus(a), StepStatus(b))
    check_step_transition(StepStatus.POLICY_CHECKED, StepStatus.EXECUTING)  # auto-allowed


def test_run_replan_and_pause_paths() -> None:
    check_run_transition(RunStatus.RUNNING, RunStatus.PLANNING)
    check_run_transition(RunStatus.RUNNING, RunStatus.PAUSED_FOR_APPROVAL)
    check_run_transition(RunStatus.PAUSED_FOR_APPROVAL, RunStatus.RUNNING)
    with pytest.raises(IllegalTransitionError):
        check_run_transition(RunStatus.COMPLETED, RunStatus.RUNNING)
    with pytest.raises(IllegalTransitionError):
        check_run_transition(RunStatus.CREATED, RunStatus.COMPLETED)
