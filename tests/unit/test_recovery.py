from datetime import timedelta

import pytest
from tests.engine_flow import EMAIL_PLAN, INTERNAL_PLAN, EngineFlow
from tests.flow import Crash

from gawe.core.statemachine import RunStatus
from gawe.engine.recovery import ACTIVE_STATUSES, resume_interrupted


def test_a_run_left_mid_execution_is_finished_without_a_second_effect() -> None:
    f = EngineFlow([INTERNAL_PLAN], lease=timedelta(minutes=5))
    f.connector.script = [None, Crash()]
    f.start()
    with pytest.raises(Crash):
        f.orch.advance("run1")  # the process "dies" here
    f.clock.advance(minutes=10)  # its lease lapses

    resumed = resume_interrupted(f.runs, lambda run_id: f.orch.advance(run_id))

    assert resumed == ["run1"]
    assert f.runs.get("run1").status is RunStatus.COMPLETED
    assert len(f.connector.effects) == 2  # each step's effect happened exactly once


def test_running_it_twice_changes_nothing() -> None:
    f = EngineFlow([INTERNAL_PLAN])
    f.start()
    resume_interrupted(f.runs, lambda run_id: f.orch.advance(run_id))
    effects = len(f.connector.effects)
    assert resume_interrupted(f.runs, lambda run_id: f.orch.advance(run_id)) == []  # finished
    assert len(f.connector.effects) == effects


def test_a_run_waiting_for_approval_stays_waiting_and_an_approved_one_moves_on() -> None:
    f = EngineFlow([EMAIL_PLAN])
    f.start()
    f.orch.advance("run1")
    resume_interrupted(f.runs, f.orch.advance)
    assert f.runs.get("run1").status is RunStatus.PAUSED_FOR_APPROVAL
    assert len(f.connector.effects) == 1  # the email has not been sent

    f.approve_open()  # approved just before a restart; nobody drove the run afterwards
    resume_interrupted(f.runs, f.orch.advance)
    assert f.runs.get("run1").status is RunStatus.COMPLETED


def test_finished_runs_are_not_touched() -> None:
    f = EngineFlow([INTERNAL_PLAN])
    f.start()
    f.orch.advance("run1")
    seen: list[str] = []
    assert resume_interrupted(f.runs, seen.append) == []
    assert seen == []


def test_one_failing_submit_does_not_stop_the_others() -> None:
    f = EngineFlow([INTERNAL_PLAN, INTERNAL_PLAN])
    f.start(run_id="a")
    f.start(run_id="b")
    seen: list[str] = []

    def submit(run_id: str) -> None:
        if run_id == "a":
            raise RuntimeError("queue full")
        seen.append(run_id)

    assert resume_interrupted(f.runs, submit) == ["b"]
    assert seen == ["b"]


def test_the_limit_bounds_one_pass_and_oldest_runs_go_first() -> None:
    f = EngineFlow([INTERNAL_PLAN] * 3)
    for run_id in ("a", "b", "c"):
        f.clock.advance(seconds=1)
        f.start(run_id=run_id)
    assert resume_interrupted(f.runs, lambda _id: None, limit=2) == ["a", "b"]


def test_active_statuses_are_exactly_the_non_terminal_ones() -> None:
    from gawe.core.statemachine import RUN_TERMINAL

    assert set(ACTIVE_STATUSES) == set(RunStatus) - set(RUN_TERMINAL)
