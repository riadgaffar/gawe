"""M26 S2: an undo record is written before each rating write and armed only by read-back."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.flow import Crash
from tests.unit.test_maintenance_runner import restart
from tests.unit.test_write_readback import ALICE, BOB, CRM, Env

from gawe.api.wiring import load_app_rules
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.core.undo import (
    InMemoryUndoStore,
    UndoConflictError,
    UndoRecord,
    settled,
    undo_identity,
)

LOW = {**CRM, "risk_level": "low"}  # needs legal, so there is a gap before the write


def records(e: Env) -> list[UndoRecord]:
    return e.stores.undo.for_run(ALICE.tenant_id, e.run_id)


def revision(e: Env) -> str:
    return e.clm.current_assessment("C-1001")[1]


def test_the_record_exists_before_the_write_and_is_armed_by_read_back(tmp_path: Path) -> None:
    e = Env(tmp_path)
    seen: list[list[UndoRecord]] = []
    e.clm.before_guarded_write = lambda: seen.append(records(e))
    assert e.execute() is RunStatus.COMPLETED
    ((before,),) = seen  # one write, and its record was already there
    assert before.state == "pending" and before.result_version is None
    (record,) = records(e)
    step = e.stores.steps.get(e.run_id, "write")
    assert record.state == "armed" and record.result_version == revision(e)
    assert record.undo_id == undo_identity(ALICE.tenant_id, e.run_id, "write", step.action_hash)
    assert (record.tool, record.tool_version) == ("crm.write_assessment", "4.0.0")
    assert record.subject == {"contract_id": "C-1001"}
    assert record.prior is None  # the seed has no rating
    assert record.bound_version == step.resolved_args["record"]["contract_version"]
    assert record.bound_version != record.result_version


def test_prior_is_the_rating_the_write_replaced(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.clm.replace_contract("C-1001", {"risk_level": "high", "risk_summary": "earlier review"})
    assert e.execute() is RunStatus.COMPLETED
    step = e.stores.steps.get(e.run_id, "write")
    assert step.resolved_args["prior"] == {"risk_level": "high", "summary": "earlier review"}
    (record,) = records(e)
    assert record.prior == {"risk_level": "high", "summary": "earlier review"}
    assert e.clm.current_assessment("C-1001")[0] == {
        "risk_level": "medium",
        "summary": CRM["summary"],
    }


def test_a_change_after_preparation_refuses_the_write_and_the_record_never_arms(
    tmp_path: Path,
) -> None:
    e = Env(tmp_path, args=LOW)
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    # Someone else rates the contract after preparation read its (absent) rating.
    e.clm.replace_contract("C-1001", {"risk_level": "high", "risk_summary": "someone else"})
    e.approve()
    e.advance()
    step = e.stores.steps.get(e.run_id, "write")
    assert step.status is StepStatus.FAILED and step.error == "record_changed"
    (record,) = records(e)
    assert record.state == "unavailable" and record.reason == "not_dispatched"
    assert record.prior is None  # what preparation saw; the write it describes never landed


def test_a_mismatch_leaves_the_record_unavailable(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.target.applied_override = {"risk_level": "high"}  # the system applies something else
    e.execute()
    assert e.stores.steps.get(e.run_id, "write").status is StepStatus.VERIFICATION_BLOCKED
    (record,) = records(e)
    assert record.state == "unavailable" and record.reason == "readback_mismatch"


def test_an_unknown_outcome_leaves_the_record_unavailable(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.target.visible_at = e.clock.now + timedelta(days=1)  # never visible before the deadline
    assert e.advance() is RunStatus.WAITING_VERIFICATION
    assert records(e)[0].state == "pending"
    e.clock.advance(minutes=6)
    assert e.advance() is RunStatus.UNKNOWN
    (record,) = records(e)
    assert record.state == "unavailable" and record.reason == "outcome_unknown"


def test_a_delayed_agreement_arms_it_once(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    assert e.advance() is RunStatus.WAITING_VERIFICATION
    e.clock.advance(seconds=61)
    assert e.advance() is RunStatus.COMPLETED
    (record,) = records(e)
    assert record.state == "armed" and record.revision == 1


def test_a_crash_after_the_record_before_the_intent_recovers_to_one_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path)
    resolver = e.orch._executor.verification
    assert resolver is not None
    original = resolver.undo.add

    def after_record(record: UndoRecord) -> bool:
        original(record)
        raise Crash()

    monkeypatch.setattr(resolver.undo, "add", after_record)
    with pytest.raises(Crash):
        e.advance()
    assert e.calls == [] and records(e)[0].state == "pending"
    monkeypatch.undo()
    assert e.advance() is RunStatus.COMPLETED
    (record,) = records(e)  # adding it again was a no-op
    assert record.state == "armed" and len(e.calls) == 1


def test_a_crash_after_the_result_is_saved_still_arms_on_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path)
    resolver = e.orch._executor.verification
    assert resolver is not None
    original = resolver.jobs.replace

    def after_result(job: Any, **kwargs: Any) -> Any:
        value = original(job, **kwargs)
        if job.result:
            raise Crash()
        return value

    monkeypatch.setattr(resolver.jobs, "replace", after_result)
    with pytest.raises(Crash):
        e.advance()
    assert records(e)[0].state == "pending"
    monkeypatch.undo()
    e.clock.advance(seconds=31)  # past the crashed driver's lease
    service = restart(e)
    service.recover_interrupted()
    service._orch.advance(e.run_id)
    (record,) = records(e)
    assert record.state == "armed" and record.result_version == revision(e)
    assert len(e.calls) == 1


def test_a_model_supplied_prior_never_reaches_policy(tmp_path: Path) -> None:
    """The proposal schema has no `prior`, so the plan is refused before any step exists."""
    fake = {"prior": {"risk_level": "low", "summary": "made up"}}
    e = Env(tmp_path, args={**LOW, **fake}, plan_attempts=2)
    assert e.advance() is RunStatus.FAILED
    run = e.stores.runs.get(e.run_id)
    assert run.plan is None and e.calls == [] and records(e) == []


def test_the_approval_card_shows_the_prior_rating(tmp_path: Path) -> None:
    e = Env(tmp_path, args=LOW)
    e.clm.replace_contract("C-1001", {"risk_level": "high", "risk_summary": "earlier review"})
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    (item,) = e.service.inbox(BOB)
    typed = {f.path: f.value for f in item.typed_fields}
    assert typed["risk_level"] == "low" and typed["prior.risk_level"] == "high"
    free = {f.path: f for f in item.free_text_fields}
    assert free["prior.summary"].length == len("earlier review")


def test_another_tenant_never_reads_a_record(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.execute()
    (record,) = records(e)
    assert e.stores.undo.get("t2", record.undo_id) is None
    assert e.stores.undo.for_run("t2", e.run_id) == []


def test_settling_never_changes_a_record_that_left_pending() -> None:
    record = UndoRecord(
        undo_id="a" * 64,
        tenant_id="t1",
        run_id="r",
        step_id="s",
        tool="crm.write_assessment",
        tool_version="3.0.0",
        action_hash="b" * 64,
        operation_key="k",
        subject={"contract_id": "C-1001"},
        prior=None,
        bound_version="v1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    armed = settled(record, "agreement", {"result_version": "v2"})
    assert armed is not None and (armed.state, armed.result_version) == ("armed", "v2")
    assert settled(armed, "disagreement", {}) is None
    missing = settled(record, "agreement", {})
    assert missing is not None and missing.reason == "result_version_unobserved"
    store = InMemoryUndoStore()
    assert store.add(record) and not store.add(record)
    with pytest.raises(UndoConflictError):
        store.add(record.model_copy(update={"prior": {"risk_level": "low", "summary": ""}}))
    store.replace(armed, expected_revision=0)
    with pytest.raises(UndoConflictError):
        store.replace(armed, expected_revision=0)  # stale
    with pytest.raises(UndoConflictError):
        store.replace(
            armed.model_copy(update={"bound_version": "v9", "revision": 2}), expected_revision=1
        )


def test_startup_refuses_an_undoable_write_whose_target_cannot_reverse_it(tmp_path: Path) -> None:
    e = Env(tmp_path)
    key = ("crm.write_assessment", "3.0.0")
    without = {**e.bundle.guarantees, key: e.bundle.guarantees[key] - {"reversible"}}
    with pytest.raises(ValueError, match="doesn't declare reversible"):
        load_app_rules(replace(e.bundle, guarantees=without))
    retired = {("crm.write_assessment", "2.0.0"): e.bundle.undoable[key]}
    with pytest.raises(ValueError, match="isn't a current write"):
        load_app_rules(replace(e.bundle, undoable=retired))


def test_a_prepare_only_rating_gets_no_undo_record(tmp_path: Path) -> None:
    e = Env(tmp_path)
    service = restart(
        replace_bundle(
            e,
            prepare_only={
                ("crm.write_assessment", v): "legal_reviewer" for v in ("3.0.0", "4.0.0")
            },
        )
    )
    resolver = service._orch._executor.verification
    assert resolver is not None and resolver.undoable == {}


def replace_bundle(e: Env, **changes: Any) -> Env:
    e.bundle = replace(e.bundle, **changes)
    return e
