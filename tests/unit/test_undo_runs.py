"""M26 reversal integration through the assembled service, without models or connectors."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.unit.test_write_readback import ALICE, BOB, Env

from gawe.api.auth import Identity
from gawe.api.errors import BadRequestError, ForbiddenError, NotFoundError
from gawe.api.undo import UndoRefused
from gawe.core.statemachine import StepStatus
from gawe.core.undo import reversal_key


def rated(path: Path, prior: str | None = "high", rating: str = "high") -> Env:
    e = Env(path, args={"contract_id": "C-1001", "risk_level": rating, "summary": "new"})
    if prior:
        e.clm.set_assessment(
            "seed", "C-1001", prior, "before", contract_version=e.clm.contract_version("C-1001")
        )
    e.advance()
    return e


def approve(e: Env, run_id: str) -> None:
    pending = next(a for a in e.service.inbox(BOB) if a.run_id == run_id)
    e.service.decide(BOB, pending.approval_id, action_hash=pending.action_hash, decision="approved")


def test_automatic_restore_and_link_events(tmp_path: Path) -> None:
    e = rated(tmp_path)
    undone = e.service.request_undo(ALICE, e.run_id, "write")
    assert undone.status == "completed"
    assert e.clm.current_assessment("C-1001")[0] == {"risk_level": "high", "summary": "before"}
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    assert record.state == "used"
    assert e.clm.operations.read(reversal_key(record.operation_key), e.clock.now)
    events = e.stores.audit.events(e.run_id)
    assert [v.type for v in events][-2:] == ["step.undo_requested", "step.undone"]
    with pytest.raises(UndoRefused, match="already_undone"):
        e.service.request_undo(ALICE, e.run_id, "write")
    with pytest.raises(UndoRefused, match="undo_of_undo"):
        e.service.request_undo(ALICE, undone.run_id, "restore")


@pytest.mark.parametrize("prior", ["low", None])
def test_low_and_absent_need_legal(tmp_path: Path, prior: str | None) -> None:
    e = rated(tmp_path, prior)
    undone = e.service.request_undo(ALICE, e.run_id, "write")
    assert undone.status == "paused_for_approval"
    assert e.clm.current_assessment("C-1001")[0]["risk_level"] == "high"
    approve(e, undone.run_id)
    assert e.service.get_run(ALICE, undone.run_id).status == "completed"
    current = e.clm.current_assessment("C-1001")[0]
    assert current is None if prior is None else current["risk_level"] == prior


def test_away_and_back_refuses(tmp_path: Path) -> None:
    e = rated(tmp_path)
    e.clm.replace_contract("C-1001", {"note": "away"})
    e.clm.replace_contract("C-1001", {"note": "back"})
    undone = e.service.request_undo(ALICE, e.run_id, "write")
    step = e.stores.steps.get(undone.run_id, "restore")
    assert step.status is StepStatus.FAILED
    assert step.error == "record_changed"
    assert e.stores.undo.for_run("t1", e.run_id)[0].state == "failed"
    assert e.stores.audit.events(e.run_id)[-1].type == "step.undo_failed"


def test_tenant_role_and_profile_boundaries(tmp_path: Path) -> None:
    e = rated(tmp_path)
    with pytest.raises(NotFoundError):
        e.service.request_undo(Identity("other", "u", ALICE.roles), e.run_id, "write")
    with pytest.raises(NotFoundError):
        e.service.request_undo(ALICE, e.run_id, "missing")
    with pytest.raises(ForbiddenError):
        e.service.request_undo(BOB, e.run_id, "write")
    with pytest.raises(BadRequestError):
        e.service.start_run(ALICE, objective="undo", profile="undo")


def test_concurrent_requests_create_one_run(tmp_path: Path) -> None:
    e = rated(tmp_path)

    def request() -> str:
        try:
            return e.service.request_undo(ALICE, e.run_id, "write").run_id
        except UndoRefused as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(), range(2)))
    assert results.count("already_undone") == 1
    assert len(e.stores.runs.list_for_tenant("t1")) == 2


def test_request_crash_recovers_from_durable_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = rated(tmp_path)
    undo = e.service._undo
    assert undo is not None
    original = undo.ensure

    def crash(job):  # type: ignore[no-untyped-def]
        raise RuntimeError("crash before run creation")

    monkeypatch.setattr(undo, "ensure", crash)
    with pytest.raises(RuntimeError, match="crash"):
        e.service.request_undo(ALICE, e.run_id, "write")
    monkeypatch.setattr(undo, "ensure", original)
    e.service.maintenance_runner.tick()
    assert e.stores.undo.for_run("t1", e.run_id)[0].state == "used"
    assert len(e.stores.runs.list_for_tenant("t1")) == 2


@pytest.mark.parametrize(
    "tool", ["email.send", "esign.send_for_signature", "iam.grant_access", "billing.replay_events"]
)
def test_non_reversible_tools_start_no_run(tmp_path: Path, tool: str) -> None:
    from gawe.core.records import StepRecord

    e = rated(tmp_path)
    e.stores.steps.add(
        StepRecord(run_id=e.run_id, step_id="other", tool=tool, tool_version="2.0.0")
    )
    before = e.stores.runs.list_for_tenant("t1")
    with pytest.raises(UndoRefused, match="not_reversible"):
        e.service.request_undo(ALICE, e.run_id, "other")
    assert e.stores.runs.list_for_tenant("t1") == before


@pytest.mark.parametrize("state", ["pending", "unavailable"])
def test_unconfirmed_original_starts_no_undo(tmp_path: Path, state: str) -> None:
    from gawe.core.undo import UndoRecord

    e = rated(tmp_path)
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    e.stores.undo.replace(
        UndoRecord(
            **{
                **record.model_dump(),
                "state": state,
                "result_version": None,
                "reason": "outcome_unknown" if state == "unavailable" else None,
                "revision": record.revision + 1,
            }
        ),
        expected_revision=record.revision,
    )
    with pytest.raises(UndoRefused, match="undo_unavailable"):
        e.service.request_undo(ALICE, e.run_id, "write")
    assert len(e.stores.runs.list_for_tenant("t1")) == 1


def test_unreadable_record_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    e = rated(tmp_path)

    def unreadable(*args):  # type: ignore[no-untyped-def]
        raise OSError("unreadable")

    monkeypatch.setattr(e.stores.undo, "for_run", unreadable)
    with pytest.raises(UndoRefused, match="undo_unavailable"):
        e.service.request_undo(ALICE, e.run_id, "write")
    assert len(e.stores.runs.list_for_tenant("t1")) == 1


def test_high_to_low_undo_restores_high_without_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gawe.core.audit import verify_chain

    e = rated(tmp_path, "high", "low")
    approve(e, e.run_id)

    def forbidden(*args):  # type: ignore[no-untyped-def]
        raise AssertionError("undo must never call the planner")

    monkeypatch.setattr(e.orch._planner, "plan", forbidden)
    undone = e.service.request_undo(ALICE, e.run_id, "write")
    assert undone.status == "completed"
    assert e.clm.current_assessment("C-1001")[0]["risk_level"] == "high"
    assert verify_chain(e.stores.audit.events(e.run_id)).ok
    assert verify_chain(e.stores.audit.events(undone.run_id)).ok


@pytest.mark.parametrize("prior", [None, "low", "high"])
def test_later_rating_uses_restored_history(tmp_path: Path, prior: str | None) -> None:
    from gawe.core.models import Plan, PlanStep

    e = rated(tmp_path, prior)
    e.clock.advance(seconds=1)
    undone = e.service.request_undo(ALICE, e.run_id, "write")
    if undone.status == "paused_for_approval":
        approve(e, undone.run_id)
    e.clock.advance(seconds=1)
    run = e.orch.start_run(
        tenant_id="t1",
        acting_user_id="alice",
        roles=("case_manager",),
        objective="later",
        profile="contract_review",
    )
    e.stores.runs.update(
        run.run_id,
        plan_version=1,
        plan=Plan(
            steps=(
                PlanStep(
                    id="write",
                    tool="crm.write_assessment",
                    args={"contract_id": "C-1001", "risk_level": "medium", "summary": "later"},
                ),
            )
        ),
    )
    later = e.orch.advance(run.run_id)
    assert later.status.value == ("paused_for_approval" if prior == "high" else "completed")


def test_restore_to_none_preserves_later_pending_ratings() -> None:
    from datetime import UTC, datetime

    from gawe.apps.contract_review import rating_history
    from gawe.core.history import HistoryEntry
    from gawe.core.runs import RunRecord

    now = datetime.now(UTC)

    def entry(level: str, status: StepStatus) -> HistoryEntry:
        return HistoryEntry("r", "s", "1.0.0", "a" * 64, status, {"risk_level": level}, now)

    run = RunRecord(
        run_id="r", tenant_id="t", acting_user_id="u", objective="o", profile="p", created_at=now
    )
    entries = [entry("high", StepStatus.SUCCEEDED), entry("none", StepStatus.SUCCEEDED)]
    assert rating_history(entries, {}, run) == {"lower_ratings": []}
    entries.append(entry("high", StepStatus.AWAITING_APPROVAL))
    assert rating_history(entries, {}, run) == {"lower_ratings": ["low", "medium"]}


@pytest.mark.parametrize("boundary", ["run_insert", "accept", "readback_result"])
def test_undo_crash_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from tests.flow import Crash

    from gawe.core.undo import reversal_run_id

    e = rated(tmp_path)
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    resolver = e.orch._executor.verification
    assert resolver is not None
    if boundary == "run_insert":
        target, name = e.stores.runs, "add"
    elif boundary == "accept":
        target, name = e.clm.operations, "accept"
    else:
        target, name = resolver.jobs, "replace"
    original = getattr(target, name)

    def crash(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)
        if boundary != "readback_result" or (args[0].kind == "write_readback" and args[0].result):
            raise Crash()
        return result

    monkeypatch.setattr(target, name, crash)
    with pytest.raises(Crash):
        e.service.request_undo(ALICE, e.run_id, "write")
    monkeypatch.undo()
    e.clock.advance(seconds=31)
    e.service.maintenance_runner.tick()
    assert e.service.get_run(ALICE, reversal_run_id(record)).status == "completed"
    assert e.stores.undo.for_run("t1", e.run_id)[0].state == "used"
    assert (
        sum(
            v.get("operation_key") == reversal_key(record.operation_key)
            for v in e.clm.operations.records()
        )
        == 1
    )
    assert [ev.type for ev in e.stores.audit.events(reversal_run_id(record))].count(
        "run.created"
    ) == 1
    assert [ev.type for ev in e.stores.audit.events(e.run_id)].count("step.undone") == 1


def test_cancel_before_materialization_settles_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = rated(tmp_path)
    monkeypatch.setattr(e.service, "kick", lambda _: None)
    undone = e.service.request_undo(ALICE, e.run_id, "write")
    e.service.cancel_run(ALICE, undone.run_id)
    e.service.maintenance_runner.tick()
    assert e.stores.undo.for_run("t1", e.run_id)[0].state == "failed"
    assert e.stores.audit.events(e.run_id)[-1].type == "step.undo_failed"


def test_http_undo_and_downstream_view(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from gawe.api.app import create_app
    from gawe.api.auth import TokenAuthenticator, hash_token

    e = Env(
        tmp_path,
        steps=[
            {
                "id": "write",
                "tool": "crm.write_assessment",
                "args": {"contract_id": "C-1001", "risk_level": "high", "summary": "new"},
            },
            {
                "id": "next",
                "tool": "clm.get_contract",
                "args": {"contract_id": {"$ref": "write.output.contract_id"}},
                "depends_on": ["write"],
            },
        ],
    )
    e.clm.set_assessment(
        "seed", "C-1001", "high", "before", contract_version=e.clm.contract_version("C-1001")
    )
    e.advance()
    token = "a" * 32
    client = TestClient(
        create_app(service=e.service, authenticator=TokenAuthenticator({hash_token(token): ALICE}))
    )
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/v1/runs/{e.run_id}/steps/write/undo"
    view = client.get(url, headers=headers)
    assert view.status_code == 200
    assert view.json()["later_steps"] == [
        {"step_id": "next", "tool": "clm.get_contract", "outcome": "succeeded"}
    ]
    assert "stay" in view.json()["notice"]
    assert client.post(url, headers=headers).status_code == 201
    assert client.post(url, headers=headers).json()["error"]["code"] == "already_undone"


def test_restore_racing_another_write_has_one_winner(tmp_path: Path) -> None:
    import threading

    from gawe.connectors.base import ToolPreDispatchError

    e = rated(tmp_path)
    version = e.clm.contract_version("C-1001")
    barrier = threading.Barrier(2)
    e.clm.before_guarded_write = lambda: barrier.wait(timeout=10)

    def other_write() -> str:
        try:
            e.clm.set_assessment("racer", "C-1001", "medium", "other", contract_version=version)
            return "completed"
        except ToolPreDispatchError:
            return "failed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        other = pool.submit(other_write)
        undone = pool.submit(e.service.request_undo, ALICE, e.run_id, "write")
        results = [other.result(), undone.result().status]
    assert sorted(results) == ["completed", "failed"]
