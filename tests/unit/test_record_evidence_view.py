"""M27 S5: only bounded, action-bound persisted evidence reaches comparison cards."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.test_apps_event_replay import Desk, replay_ids
from tests.unit.test_refusal_observations import fallback
from tests.unit.test_trusted_versions import legacy_job
from tests.unit.test_undo_runs import rated
from tests.unit.test_write_readback import ALICE, BOB, Env

from gawe.api.app import create_app
from gawe.api.auth import Identity, TokenAuthenticator, hash_token
from gawe.api.errors import NotFoundError
from gawe.apps.record_binding import CONTRACT_FIELDS
from gawe.connectors.base import ConnectorError
from gawe.core.canonical import action_hash
from gawe.core.record_changes import CapturedField, FieldSpec, SnapshotSpec


def view(e: Env) -> Any:
    return e.service.get_run(ALICE, e.run_id).steps[0].record_evidence


def test_approval_http_shows_saved_fields_without_refreshing_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(
        tmp_path, args={"contract_id": "C-1001", "risk_level": "low", "summary": "private prose"}
    )
    e.advance()
    e.clm.replace_contract("C-1001", {"deal_value": 999999})
    monkeypatch.setattr(
        e.clm, "current_contract", lambda _: pytest.fail("display must not read the target")
    )
    token = "a" * 32
    client = TestClient(
        create_app(service=e.service, authenticator=TokenAuthenticator({hash_token(token): BOB}))
    )
    response = client.get("/v1/approvals", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    evidence = response.json()[0]["record_evidence"]
    row = evidence["records"][0]
    assert row["state"] == "captured" and row["snapshot"]["fields"][0]["value"] == 45000
    assert row["observation"] is None and evidence["code"] is None
    assert "private prose" not in str(evidence)
    assert e.service.inbox(BOB)[0].record_evidence.model_dump(mode="json") == evidence


@pytest.mark.parametrize(
    "system,change,state",
    [
        ("crm", {"deal_value": 150000}, "changed"),
        ("crm", {"note": "secret"}, "unchanged"),
        ("iam", {"department": "secret"}, "no_fields_tracked"),
    ],
)
def test_guard_refusal_states_are_bound_and_stable(
    tmp_path: Path, system: str, change: dict[str, Any], state: str
) -> None:
    e = Env(tmp_path, system)
    if system == "iam":
        e.iam.before_guarded_write = lambda: e.iam.replace_user("dana.ortiz@acme.example", change)
    else:
        e.clm.before_guarded_write = lambda: e.clm.replace_contract("C-1001", change)
    e.execute()
    first = view(e)
    target = first.records[0]
    assert target.state == state and target.observation.timing == "at_guard"
    assert first.action_hash == e.stores.steps.get(e.run_id, "write").action_hash
    assert first.code == "record_changed" and first.coverage == "complete"
    assert "secret" not in first.model_dump_json()
    assert e.service.verification_evidence(ALICE, e.run_id)[0].record_evidence == first
    e.clm.replace_contract("C-1001", {"deal_value": 250000})
    assert view(e) == first and len(e.calls) == 1
    with pytest.raises(NotFoundError):
        e.service.get_run(Identity("other", "u", ALICE.roles), e.run_id)


@pytest.mark.parametrize(
    "mode,state",
    [
        ("later", "unchanged"),
        ("missing", "record_missing"),
        ("unreadable", "observation_unavailable"),
    ],
)
def test_later_missing_and_unreadable_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, state: str
) -> None:
    e = Env(tmp_path)

    def change() -> None:
        if mode != "later":

            def read(_: str) -> Any:
                if mode == "missing":
                    raise ConnectorError("contract_not_found")
                raise OSError("private error")

            monkeypatch.setattr(e.clm, "current_contract", read)

    fallback(e, change)
    e.execute()
    target = view(e).records[0]
    assert target.state == state and target.observation.timing == "after_refusal"
    assert "private error" not in view(e).model_dump_json()


def test_partial_batch_lists_unobserved_targets_without_inventing_comparisons(
    tmp_path: Path,
) -> None:
    d = Desk(tmp_path)
    impl = d.svc._orch._executor._tools["billing.replay_events"]

    def changed(args: Any, call: Any) -> Any:
        d.billing.change_event("ev-1001", {"amount": 999})
        return impl(args, call)

    d.svc._orch._executor._tools["billing.replay_events"] = changed
    result = d.run(replay_ids("ev-1001", "ev-1002"))
    evidence = result.steps[0].record_evidence
    assert evidence.coverage == "partial"
    assert [r.subject for r in evidence.records] == ["ev-1001", "ev-1002"]
    assert [r.state for r in evidence.records] == ["changed", "observation_unavailable"]
    assert evidence.records[1].observation is None and evidence.records[1].rows == ()
    assert d.billing.operations.records() == []


@pytest.mark.parametrize("mutation", ["hash", "subject", "resource", "fields", "coverage"])
def test_invalid_diagnostic_is_unavailable_and_never_leaks(tmp_path: Path, mutation: str) -> None:
    e = Env(tmp_path)
    fallback(e)
    e.execute()
    evidence = deepcopy(e.job.evidence)
    diagnostic = evidence["diagnostic"]
    if mutation == "hash":
        diagnostic["action_hash"] = "a" * 64
    elif mutation == "coverage":
        diagnostic["observations"] = []
    elif mutation == "fields":
        diagnostic["observations"][0]["fields"].append(
            {"path": "private", "value": "secret", "present": True}
        )
    else:
        diagnostic["observations"][0][mutation] = "secret"
    run = e.stores.runs.get(e.run_id)
    step = e.stores.steps.get(e.run_id, "write")
    verification = e.service.verification_evidence(ALICE, e.run_id)[0].model_copy(
        update={"evidence": evidence}
    )
    shown = e.service._record_presenter.project(run, step, verification=verification)
    assert shown.records[0].state == "observation_unavailable" and shown.coverage == "unavailable"
    assert "secret" not in shown.model_dump_json()


def test_mismatched_snapshot_hash_or_revision_is_not_presented_as_trustworthy(
    tmp_path: Path,
) -> None:
    e = rated(tmp_path)
    run = e.stores.runs.get(e.run_id)
    step = e.stores.steps.get(e.run_id, "write")
    args = deepcopy(step.resolved_args)
    args["snapshots"][0]["revision"] = "wrong"
    presenter = e.service._record_presenter
    assert presenter.project(run, step, args=args).records == ()
    digest = action_hash(
        tool=step.tool,
        version=step.tool_version,
        resolved_args=args,
        run_id=run.run_id,
        step_id=step.step_id,
    )
    shown = presenter.project(
        run, step.model_copy(update={"resolved_args": args, "action_hash": digest})
    )
    assert shown.records[0].state == "snapshot_unavailable" and shown.records[0].snapshot is None


def test_undo_provenance_requires_the_immutable_original_record(tmp_path: Path) -> None:
    e = rated(tmp_path, "low")
    run = e.service.request_undo(ALICE, e.run_id, "write")
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    shown = e.service.inbox(BOB)[0].record_evidence.records[0]
    assert shown.snapshot.basis == "confirmed_original"
    assert shown.snapshot.source_action_hash == record.action_hash
    assert shown.snapshot.revision == record.bound_version != shown.guard_revision
    e.stores.undo._records.pop(record.undo_id)
    shown = e.service.get_run(ALICE, run.run_id).steps[0].record_evidence.records[0]
    assert shown.snapshot is None and shown.state == "snapshot_unavailable"


def test_legacy_snapshotless_refusal_does_not_invent_history(tmp_path: Path) -> None:
    e = rated(tmp_path)
    job = legacy_job(e)
    e.stores.maintenance.add(job)
    e.clm.replace_contract("C-1001", {"deal_value": 150000})
    run = e.service._undo.ensure(job)
    e.service._undo.process(job)
    shown = e.service.get_run(ALICE, run.run_id).steps[0].record_evidence.records[0]
    assert shown.state == "snapshot_unavailable" and shown.snapshot is None
    assert shown.observation.fields[0].value == 150000 and shown.rows == ()


def test_missing_and_null_are_distinct_in_api_rows(tmp_path: Path) -> None:
    e = Env(tmp_path)
    fallback(e)
    e.execute()
    run = e.stores.runs.get(e.run_id)
    step = e.stores.steps.get(e.run_id, "write")
    args = deepcopy(step.resolved_args)
    args["snapshots"][0]["fields"] = [CapturedField(path="deal_value", present=False).model_dump()]
    digest = action_hash(
        tool=step.tool,
        version=step.tool_version,
        resolved_args=args,
        run_id=run.run_id,
        step_id=step.step_id,
    )
    step = step.model_copy(update={"resolved_args": args, "action_hash": digest})
    verification = e.service.verification_evidence(ALICE, e.run_id)[0]
    evidence = deepcopy(verification.evidence)
    evidence["diagnostic"]["action_hash"] = digest
    evidence["diagnostic"]["observations"][0]["fields"] = [
        CapturedField(path="deal_value", value=None).model_dump()
    ]
    verification = verification.model_copy(update={"action_hash": digest, "evidence": evidence})
    presenter = e.service._record_presenter
    key = (step.tool, step.tool_version)
    presenter.observers[key] = replace(
        presenter.observers[key],
        spec=SnapshotSpec(
            resource=CONTRACT_FIELDS.resource,
            fields=(FieldSpec(path="deal_value", kind="number", nullable=True, required=False),),
        ),
    )
    shown = presenter.project(run, step, verification=verification)
    row = shown.records[0].rows[0]
    assert (
        row.changed
        and not row.before.present
        and row.observed.present
        and row.observed.value is None
    )
