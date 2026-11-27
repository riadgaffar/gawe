"""M27 S2: same-read authorization snapshots, legacy execution and unchanged write guards."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from tests.unit.test_apps_event_replay import OPERATOR, Desk, replay_ids
from tests.unit.test_maintenance_runner import restart
from tests.unit.test_record_versions import GATED
from tests.unit.test_write_readback import ALICE, BOB, Env

from gawe.apps import contract_review
from gawe.apps.contract_review.record_preparation import ContractSnapshotPreparer
from gawe.core.canonical import action_hash
from gawe.core.preparation import PreparationError
from gawe.core.record_changes import RecordSnapshot
from gawe.core.records import StepRecord
from gawe.core.statemachine import RunStatus


@pytest.mark.parametrize(
    "system,version,fields",
    [
        ("crm", "4.0.0", {"deal_value": 45000}),
        ("esign", "3.0.0", {"deal_value": 45000}),
        ("iam", "3.0.0", {}),
    ],
)
def test_new_versions_bind_only_declared_fields_and_verify_effects(
    tmp_path: Path, system: str, version: str, fields: dict[str, Any]
) -> None:
    e = Env(tmp_path, system)
    assert e.execute() is RunStatus.COMPLETED
    step = e.stores.steps.get(e.run_id, "write")
    assert step.tool_version == version
    (raw,) = step.resolved_args["snapshots"]
    snapshot = RecordSnapshot.model_validate(raw)
    assert {f.path: f.value for f in snapshot.fields} == fields
    assert all(f.present for f in snapshot.fields)
    assert snapshot.subject == step.resolved_args.get(
        "contract_id", step.resolved_args.get("email")
    )
    assert snapshot.revision == next(iter(step.resolved_args["record"].values()))
    assert snapshot.basis == "preparation" and snapshot.source_action_hash is None
    assert snapshot.captured_at.utcoffset() is not None
    assert e.job.result["outcome"] == "agreement" and len(e.calls) == 1
    changed = deepcopy(step.resolved_args)
    changed["snapshots"][0]["revision"] = "tampered"
    assert (
        action_hash(
            tool=step.tool,
            version=version,
            resolved_args=changed,
            run_id=e.run_id,
            step_id=step.step_id,
        )
        != step.action_hash
    )


def test_policy_uses_the_preparation_read_even_if_the_contract_changes_before_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path, args=GATED["crm"])
    read = e.clm.current_contract
    calls = []

    def racing_read(subject: str) -> Any:
        record, revision = read(subject)
        calls.append(subject)
        e.clm.replace_contract(subject, {"deal_value": 1})
        return record, revision

    monkeypatch.setattr(e.clm, "current_contract", racing_read)
    monkeypatch.setattr(e.clm, "deal_values", lambda: pytest.fail("second policy resource read"))
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    before = e.stores.steps.get(e.run_id, "write")
    assert calls == ["C-1002"]
    assert "low-risk-large-deal-needs-legal" in before.policy_decision.rule_ids
    assert before.resolved_args["snapshots"][0]["fields"][0]["value"] == 250000
    e.approve()
    after = e.stores.steps.get(e.run_id, "write")
    assert after.error == "record_changed" and after.action_hash == before.action_hash
    assert after.resolved_args == before.resolved_args and len(calls) == 1
    assert e.clm.operations.records() == [] and len(e.calls) == 1


def test_capture_ignores_model_visible_output_overrides_and_uses_one_read_for_prior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path)
    e.clm.set_assessment(
        "seed", "C-1001", "low", "before", contract_version=e.clm.contract_version("C-1001")
    )
    e.clm.output_overrides["C-1001"] = {"deal_value": 1, "risk_level": "high"}
    monkeypatch.setattr(e.clm, "current_assessment", lambda _: pytest.fail("split prior read"))
    monkeypatch.setattr(e.clm, "contract_version", lambda _: pytest.fail("split revision read"))
    prepared = ContractSnapshotPreparer(e.clm, assessment=True, clock=e.clock)(
        e.steps[0]["args"], reapproval=False
    )
    assert prepared.args["prior"] == {"risk_level": "low", "summary": "before"}
    raw = prepared.args["snapshots"][0]
    assert raw["fields"][0]["value"] == 45000
    assert raw["revision"] == prepared.args["record"]["contract_version"]
    assert RecordSnapshot.model_validate(raw).captured_at == e.clock.now


@pytest.mark.parametrize("value", [None, True, "45000", {}, float("nan")])
def test_unreadable_declared_fields_fail_before_policy_or_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    e = Env(tmp_path)
    monkeypatch.setattr(e.clm, "current_contract", lambda _: ({"deal_value": value}, "v1"))
    assert e.advance() is RunStatus.FAILED
    step = e.stores.steps.get(e.run_id, "write")
    assert step.error == "record_version_unavailable" and step.policy_decision is None
    assert e.calls == [] and e.clm.operations.records() == []


@pytest.mark.parametrize("system", ["crm", "esign", "iam"])
def test_model_cannot_supply_a_snapshot(tmp_path: Path, system: str) -> None:
    e = Env(tmp_path, system, args={**GATED[system], "snapshots": []}, plan_attempts=2)
    assert e.advance() is RunStatus.FAILED
    assert e.stores.runs.get(e.run_id).plan is None and e.calls == []


@pytest.mark.parametrize("system,version", [("crm", "3.0.0"), ("esign", "2.0.0"), ("iam", "2.0.0")])
def test_persisted_old_step_and_approval_keep_their_schema_hash_and_execution(
    tmp_path: Path, system: str, version: str
) -> None:
    e = Env(tmp_path, system, args=GATED[system])
    e.stores.steps.add(
        StepRecord(
            run_id=e.run_id,
            step_id="write",
            tool=e.steps[0]["tool"],
            tool_version=version,
            args_template=e.steps[0]["args"],
        )
    )
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    before = e.stores.steps.get(e.run_id, "write")
    assert "snapshots" not in before.resolved_args and before.tool_version == version
    service = restart(e)
    (item,) = service.inbox(BOB)
    service.decide(BOB, item.approval_id, decision="approved", action_hash=item.action_hash)
    after = e.stores.steps.get(e.run_id, "write")
    assert service.get_run(ALICE, e.run_id).status == "completed"
    assert after.tool_version == version and after.action_hash == before.action_hash
    assert after.resolved_args == before.resolved_args
    assert len(e.calls) == 1


def test_replay_snapshots_use_the_frozen_batch_read_and_policy_totals(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    view = d.run(replay_ids("ev-1001", "ev-1002"))
    step = d.step(view.run_id)
    assert step.tool_version == "2.0.0" and view.status == "completed"
    args = step.resolved_args
    snapshots = [RecordSnapshot.model_validate(v) for v in args["snapshots"]]
    for record, captured in zip(args["batch"], snapshots, strict=True):
        assert captured.resource == "billing.event"
        assert (captured.subject, captured.revision) == (record["event_id"], record["revision"])
        assert {f.path: f.value for f in captured.fields} == {
            k: record[k] for k in ("customer", "amount")
        }
    assert args["total"] == sum(s.fields[1].value for s in snapshots)
    assert args["customer_count"] == len({s.fields[0].value for s in snapshots})


def test_legacy_replay_materialization_keeps_the_original_schema(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    run = d.svc._orch.start_run(
        tenant_id=OPERATOR.tenant_id,
        acting_user_id=OPERATOR.user_id,
        roles=tuple(OPERATOR.roles),
        objective="old replay",
        profile="event_replay",
    )
    planned = replay_ids("ev-1001")
    d.stores.steps.add(
        StepRecord(
            run_id=run.run_id,
            step_id="replay",
            tool=planned["tool"],
            tool_version="1.0.0",
            args_template=planned["args"],
        )
    )
    d.provider._script.append({"steps": [planned]})
    result = d.svc._orch.advance(run.run_id)
    assert result.status is RunStatus.COMPLETED
    assert d.step(run.run_id).tool_version == "1.0.0"
    assert "snapshots" not in d.step(run.run_id).resolved_args


def test_captured_policy_values_validate_binding_and_daily_inputs_stay_current(
    tmp_path: Path,
) -> None:
    e = Env(tmp_path, args=GATED["crm"])
    e.advance()
    run = e.stores.runs.get(e.run_id)
    args = e.stores.steps.get(e.run_id, "write").resolved_args
    e.clm.replace_contract("C-1002", {"deal_value": 1})
    assert contract_review.captured_variables(run, args)["review"]["large_deals"] == ["C-1002"]
    assert "C-1002" not in e.bundle.variables(run)["review"]["large_deals"]
    for key, value in [("subject", "C-1001"), ("revision", "other"), ("resource", "iam.user")]:
        changed = deepcopy(args)
        changed["snapshots"][0][key] = value
        with pytest.raises(PreparationError, match="record_version_unavailable"):
            contract_review.captured_variables(run, changed)
    manifest = e.orch._registry.get("crm.write_assessment")
    assert Draft202012Validator(manifest.input_schema).is_valid(args)
    without = {k: v for k, v in args.items() if k != "snapshots"}
    assert not Draft202012Validator(manifest.input_schema).is_valid(without)
