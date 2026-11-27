"""M27 S4: original evidence in undo, never a fresh read masquerading as history."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from tests.flow import Crash
from tests.unit.test_maintenance_runner import restart
from tests.unit.test_undo_runs import approve, rated
from tests.unit.test_write_readback import ALICE, Env

from gawe.apps import contract_review
from gawe.apps.record_binding import CONTRACT_FIELDS
from gawe.core.canonical import action_hash
from gawe.core.maintenance import JobConflictError
from gawe.core.record_changes import RecordSnapshot, RefusalDiagnostic, compare_records
from gawe.core.statemachine import RunStatus
from gawe.core.undo import UndoConflictError, UndoRecord, reversal_run_id


def test_original_evidence_exists_before_dispatch_and_survives_lost_reply(tmp_path: Path) -> None:
    e = Env(tmp_path)
    before = []
    e.clm.before_guarded_write = lambda: before.extend(e.stores.undo.for_run("t1", e.run_id))
    e.target.drop_reply = True
    e.target.read_failures = 1
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    original = RecordSnapshot.model_validate(
        e.stores.steps.get(e.run_id, "write").resolved_args["snapshots"][0]
    )
    assert before[0].state == "pending" and before[0].snapshots == (original,)
    service = restart(e)
    e.clock.advance(seconds=31)
    service.recover_interrupted()
    service._orch.advance(e.run_id)
    saved = e.stores.undo.for_run("t1", e.run_id)[0]
    assert saved.state == "armed" and saved.snapshots == (original,)
    assert len(e.calls) == 1


def test_restore_carries_original_revision_time_values_and_source_hash(tmp_path: Path) -> None:
    e = rated(tmp_path)
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    source = e.stores.steps.get(e.run_id, "write")
    run = e.service.request_undo(ALICE, e.run_id, "write")
    step = e.stores.steps.get(run.run_id, "restore")
    assert step.tool_version == "2.0.0" and run.status == "completed"
    seen = RecordSnapshot.model_validate(step.resolved_args["snapshots"][0])
    original = record.snapshots[0]
    assert seen.basis == "confirmed_original" and seen.source_action_hash == source.action_hash
    assert seen.model_dump(exclude={"basis", "source_action_hash"}) == original.model_dump(
        exclude={"basis", "source_action_hash"}
    )
    assert seen.revision == record.bound_version
    assert (
        step.resolved_args["record"]["contract_version"] == record.result_version != seen.revision
    )
    assert step.action_hash == action_hash(
        tool=step.tool,
        version=step.tool_version,
        resolved_args=step.resolved_args,
        run_id=run.run_id,
        step_id="restore",
    )


def test_later_edit_compares_against_original_and_daily_inputs_stay_current(tmp_path: Path) -> None:
    e = rated(tmp_path)
    e.clm.replace_contract("C-1001", {"deal_value": 500000})
    run = e.service.request_undo(ALICE, e.run_id, "write")
    step = e.stores.steps.get(run.run_id, "restore")
    job = next(
        j for j in e.stores.maintenance.for_run("t1", run.run_id) if j.kind == "write_readback"
    )
    saved = RefusalDiagnostic.model_validate(job.evidence["diagnostic"])
    original = RecordSnapshot.model_validate(step.resolved_args["snapshots"][0])
    assert step.error == "record_changed" and original.fields[0].value == 45000
    assert compare_records(CONTRACT_FIELDS, original, saved.observations[0]).state == "changed"
    assert "C-1001" in e.bundle.variables(e.stores.runs.get(run.run_id))["review"]["large_deals"]
    assert (step.tool, step.tool_version) not in e.bundle.action_variables
    assert len(e.clm.operations.records()) == 2  # seed and original; no restore effect


@pytest.mark.parametrize("boundary", ["request", "approval"])
def test_restart_and_future_registry_upgrade_preserve_original_undo_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    e = rated(tmp_path, "low" if boundary == "approval" else "high")
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    if boundary == "request":

        def crash(_: Any) -> Any:
            raise Crash()

        monkeypatch.setattr(e.service._undo, "ensure", crash)
        with pytest.raises(Crash):
            e.service.request_undo(ALICE, e.run_id, "write")
        monkeypatch.undo()
    else:
        e.service.request_undo(ALICE, e.run_id, "write")
    service = restart(e)
    registry = service._orch._registry
    registry.register(
        registry.get("crm.restore_assessment").model_copy(update={"version": "3.0.0"})
    )
    e.clock.advance(seconds=31)
    service.maintenance_runner.tick()
    e.service = service
    if boundary == "approval":
        approve(e, reversal_run_id(record))
    step = e.stores.steps.get(reversal_run_id(record), "restore")
    assert step.tool_version == "2.0.0" and step.status.value == "succeeded"
    assert step.resolved_args["snapshots"][0]["source_action_hash"] == record.action_hash
    assert (
        step.resolved_args["snapshots"][0]["captured_at"]
        == record.snapshots[0].model_dump(mode="json")["captured_at"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_action_hash", "a" * 64),
        ("revision", "other"),
        ("subject", "C-1002"),
        ("basis", "preparation"),
        ("fields", []),
    ],
)
def test_restore_arguments_cannot_substitute_original_provenance(
    tmp_path: Path, field: str, value: Any
) -> None:
    e = rated(tmp_path, "low")
    run = e.service.request_undo(ALICE, e.run_id, "write")
    step = e.stores.steps.get(run.run_id, "restore")
    changed = deepcopy(step.resolved_args)
    changed["snapshots"][0][field] = value
    assert (
        action_hash(
            tool=step.tool,
            version=step.tool_version,
            resolved_args=changed,
            run_id=run.run_id,
            step_id="restore",
        )
        != step.action_hash
    )
    with pytest.raises(JobConflictError, match="undo identity"):
        e.orch._executor.verification.operation_key(
            step.model_copy(update={"resolved_args": changed})
        )
    assert len(e.clm.operations.records()) == 2


def test_pre_s4_record_keeps_its_serialization_and_snapshotless_restore(tmp_path: Path) -> None:
    e = rated(tmp_path)
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    old_json = record.model_dump(mode="json")
    old_json.pop("snapshots")
    old = UndoRecord.model_validate(old_json)
    assert old.model_dump(mode="json") == old_json
    # Simulate loading an S3 record, which predates durable original snapshot storage.
    e.stores.undo._records[old.undo_id] = old
    resolver = e.orch._executor.verification
    reconstructed = resolver._undo_record(e.stores.steps.get(e.run_id, "write"))
    assert reconstructed.snapshots == () and not e.stores.undo.add(reconstructed)
    run = e.service.request_undo(ALICE, e.run_id, "write")
    step = e.stores.steps.get(run.run_id, "restore")
    assert run.status == "completed" and step.tool_version == "2.0.0"
    assert step.resolved_args["snapshots"] == []
    assert e.stores.undo.for_run("t1", e.run_id)[0].snapshots == ()


def test_undo_store_rejects_changed_snapshots_and_source_hash(tmp_path: Path) -> None:
    e = rated(tmp_path)
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    for update in [{"snapshots": ()}, {"action_hash": "a" * 64}]:
        with pytest.raises(UndoConflictError):
            e.stores.undo.replace(
                record.model_copy(update={**update, "revision": record.revision + 1}),
                expected_revision=record.revision,
            )


def test_original_snapshot_projection_rejects_bad_identity_before_undo_persistence(
    tmp_path: Path,
) -> None:
    e = rated(tmp_path)
    step = e.stores.steps.get(e.run_id, "write")
    args = deepcopy(step.resolved_args)
    args["snapshots"][0]["subject"] = "C-1002"
    with pytest.raises(Exception, match="record_version_unavailable"):
        contract_review.UNDOABLE[(step.tool, step.tool_version)].snapshots(args)
    with pytest.raises(JobConflictError, match="source action"):
        e.orch._executor.verification._undo_record(step.model_copy(update={"resolved_args": args}))


def test_delayed_restart_after_record_before_intent_preserves_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path)
    save = e.stores.undo.add

    def crash(record: UndoRecord) -> bool:
        save(record)
        raise Crash()

    monkeypatch.setattr(e.stores.undo, "add", crash)
    with pytest.raises(Crash):
        e.advance()
    pending = e.stores.undo.for_run("t1", e.run_id)[0]
    assert pending.state == "pending" and e.calls == []
    monkeypatch.undo()
    e.clock.advance(minutes=1)
    service = restart(e)
    service.recover_interrupted()
    service._orch.advance(e.run_id)
    final = e.stores.undo.for_run("t1", e.run_id)[0]
    assert final.state == "armed" and len(e.calls) == 1
    assert final.snapshots == pending.snapshots and final.created_at == pending.created_at


def test_legacy_assessment_still_undoes_without_inventing_a_snapshot(tmp_path: Path) -> None:
    from gawe.core.records import StepRecord

    e = Env(tmp_path)
    e.clm.set_assessment(
        "seed", "C-1001", "high", "before", contract_version=e.clm.contract_version("C-1001")
    )
    e.stores.steps.add(
        StepRecord(
            run_id=e.run_id,
            step_id="write",
            tool="crm.write_assessment",
            tool_version="3.0.0",
            args_template=e.steps[0]["args"],
        )
    )
    e.execute()
    original = e.stores.steps.get(e.run_id, "write")
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    assert original.tool_version == "3.0.0" and record.snapshots == ()
    assert "snapshots" not in original.resolved_args
    run = e.service.request_undo(ALICE, e.run_id, "write")
    step = e.stores.steps.get(run.run_id, "restore")
    assert run.status == "completed" and step.resolved_args["snapshots"] == []
    assert e.stores.steps.get(e.run_id, "write") == original
