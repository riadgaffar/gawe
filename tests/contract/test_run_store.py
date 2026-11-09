"""The same RunStore tests against the in-memory and Postgres implementations."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from gawe.core.models import Plan, PlanStep
from gawe.core.runs import InMemoryRunStore, RunNotFoundError, RunRecord, RunStore, StaleRunError
from gawe.core.statemachine import IllegalTransitionError, RunStatus
from tests.contract.conftest import ALICE, BOB, TENANT
from tests.helpers import signer

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
PLAN = Plan(
    steps=(
        PlanStep(id="a", tool="contracts.extract_risks", args={"document_id": "d"}),
        PlanStep(
            id="b",
            tool="crm.write_assessment",
            args={"k": {"$ref": "a.output.x"}},
            depends_on=("a",),
        ),
    )
)


def make_run(**kw: object) -> RunRecord:
    base: dict[str, object] = {
        "run_id": str(uuid.uuid4()),
        "tenant_id": TENANT,
        "acting_user_id": ALICE,
        "roles": ("case_manager", "x"),
        "objective": "do it",
        "profile": "contract_review",
        "created_at": T0,
    }
    return RunRecord(**{**base, **kw})  # type: ignore[arg-type]


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Iterator[RunStore]:
    if request.param == "memory":
        yield InMemoryRunStore()
        return
    engine = request.getfixturevalue("pg_engine")  # skips when Postgres is not configured
    from gawe.db.run_store import PostgresRunStore

    conn = engine.connect()
    try:
        yield PostgresRunStore(conn)
    finally:
        conn.rollback()
        conn.close()


def decision() -> object:
    from tests.flow import Flow

    return Flow().judge("contracts.send_email", {"to": "x@partner.com", "body": "b"})


def test_add_and_get_round_trip(store: RunStore) -> None:
    run = make_run(document="some text")
    store.add(run)
    got = store.get(run.run_id)
    assert got == run
    assert got.status is RunStatus.CREATED and got.plan is None and got.plan_version == 0


def test_duplicate_and_unknown(store: RunStore) -> None:
    run = make_run()
    store.add(run)
    with pytest.raises(ValueError):
        store.add(run)
    with pytest.raises(RunNotFoundError):
        store.get(str(uuid.uuid4()))
    with pytest.raises(RunNotFoundError):
        store.update(str(uuid.uuid4()), error="x")


def test_transition_is_compare_and_set_and_checks_the_table(store: RunStore) -> None:
    run = make_run()
    store.add(run)
    moved = store.transition(run.run_id, expected=RunStatus.CREATED, to=RunStatus.PLANNING)
    assert moved.status is RunStatus.PLANNING
    with pytest.raises(StaleRunError):
        store.transition(run.run_id, expected=RunStatus.CREATED, to=RunStatus.PLANNING)
    with pytest.raises(IllegalTransitionError):
        store.transition(run.run_id, expected=RunStatus.PLANNING, to=RunStatus.COMPLETED)
    assert store.get(run.run_id).status is RunStatus.PLANNING


def test_plan_findings_and_decisions_survive_a_reload(store: RunStore) -> None:
    run = make_run(document="text")
    store.add(run)
    store.transition(run.run_id, expected=RunStatus.CREATED, to=RunStatus.PLANNING)
    store.update(run.run_id, findings={"risk": "high"}, findings_schema_id="s1", document=None)
    store.update(run.run_id, plan=PLAN, plan_version=1, planner_model="m-1")
    d = decision()
    store.update(run.run_id, decisions={"b": d})
    got = store.get(run.run_id)
    assert got.plan == PLAN and got.plan_version == 1 and got.planner_model == "m-1"
    assert got.findings == {"risk": "high"} and got.findings_schema_id == "s1"
    assert got.document is None
    assert got.decisions == {"b": d}
    assert signer().verify(got.decisions["b"])  # the stored decision is still validly signed


def test_new_plan_needs_a_higher_version(store: RunStore) -> None:
    run = make_run()
    store.add(run)
    store.transition(run.run_id, expected=RunStatus.CREATED, to=RunStatus.PLANNING)
    store.update(run.run_id, plan=PLAN, plan_version=1, planner_model="m")
    with pytest.raises((ValueError, StaleRunError)):
        store.update(run.run_id, plan=PLAN, plan_version=1, planner_model="m")
    store.update(run.run_id, plan=PLAN, plan_version=2, planner_model="m2")
    assert store.get(run.run_id).plan_version == 2


def test_update_cannot_touch_protected_fields(store: RunStore) -> None:
    run = make_run()
    store.add(run)
    for field in ("status", "tenant_id", "acting_user_id", "roles", "objective"):
        with pytest.raises(ValueError):
            store.update(run.run_id, **{field: "x"})


def test_list_for_tenant_is_newest_first_and_scoped(store: RunStore) -> None:
    older = make_run(created_at=T0)
    newer = make_run(created_at=T0 + timedelta(hours=1))
    store.add(older)
    store.add(newer)
    ids = [r.run_id for r in store.list_for_tenant(TENANT)]
    assert ids.index(newer.run_id) < ids.index(older.run_id)
    assert store.list_for_tenant("00000000-0000-0000-0000-0000000000ff") == []
    assert len(store.list_for_tenant(TENANT, limit=1)) == 1


def test_list_by_status_spans_tenants_oldest_first(store: RunStore) -> None:
    old = make_run(created_at=T0)
    new = make_run(created_at=T0 + timedelta(hours=1))
    done = make_run(created_at=T0 + timedelta(hours=2))
    for run in (new, old, done):
        store.add(run)
    store.transition(done.run_id, expected=RunStatus.CREATED, to=RunStatus.PLANNING)
    store.transition(done.run_id, expected=RunStatus.PLANNING, to=RunStatus.FAILED)
    found = [r.run_id for r in store.list_by_status([RunStatus.CREATED, RunStatus.PLANNING])]
    assert found.index(old.run_id) < found.index(new.run_id)
    assert done.run_id not in found
    assert store.list_by_status([]) == []
    assert len(store.list_by_status([RunStatus.CREATED], limit=1)) == 1


def test_finishing_clears_nothing_the_caller_did_not_ask_for(store: RunStore) -> None:
    run = make_run()
    store.add(run)
    store.transition(run.run_id, expected=RunStatus.CREATED, to=RunStatus.PLANNING)
    done = store.transition(
        run.run_id,
        expected=RunStatus.PLANNING,
        to=RunStatus.FAILED,
        error="planning_failed",
        completed_at=T0 + timedelta(minutes=1),
        document=None,
    )
    got = store.get(run.run_id)
    assert got == done and got.error == "planning_failed"
    assert BOB != ALICE  # fixtures sanity
