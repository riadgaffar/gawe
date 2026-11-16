"""Next-day review on real Postgres: items, tenant-scoped jobs, the new queries, a whole review."""

import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")

from sqlalchemy import text  # noqa: E402

from gawe.api.auth import Identity  # noqa: E402
from gawe.api.background import InlineBackground  # noqa: E402
from gawe.api.errors import ConflictError, ForbiddenError  # noqa: E402
from gawe.api.wiring import assemble, postgres_stores  # noqa: E402
from gawe.apps import contract_review  # noqa: E402
from gawe.core.audit import AuditLog  # noqa: E402
from gawe.core.review import (  # noqa: E402
    KIND,
    ReviewAlreadyDecidedError,
    ReviewItem,
    ReviewItemNotFoundError,
    ReviewKey,
    item_identity,
    review_job,
)
from gawe.core.statemachine import RunStatus  # noqa: E402
from gawe.db.audit_store import PostgresAuditStore  # noqa: E402
from gawe.db.maintenance_store import PostgresMaintenanceStore  # noqa: E402
from gawe.db.review_store import PostgresReviewStore  # noqa: E402
from gawe.db.run_store import PostgresRunStore  # noqa: E402
from gawe.llm.fake import FakeProvider  # noqa: E402
from tests.contract.conftest import ALICE, BOB, TENANT, create_run  # noqa: E402
from tests.contract.test_readback_postgres import PLAN, World  # noqa: E402

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
DAY = date(2026, 1, 1)


def committed_run(engine: Any) -> str:
    with engine.begin() as conn:
        return create_run(conn)


def item(run_id: str, reason: str = "rules_changed", **kw: Any) -> ReviewItem:
    fields: dict[str, Any] = {
        "item_id": item_identity(TENANT, DAY, run_id, "s1", "a" * 64, reason),
        "tenant_id": TENANT,
        "day": DAY,
        "run_id": run_id,
        "step_id": "s1",
        "action_hash": "a" * 64,
        "reason": reason,
        "created_at": T0,
    }
    return ReviewItem(**{**fields, **kw})


# --- the store ---------------------------------------------------------------------------------


def test_items_are_insert_once_and_tenant_scoped(pg_engine: Any) -> None:
    store = PostgresReviewStore(pg_engine)
    run_id = committed_run(pg_engine)
    first = store.add(item(run_id))
    again = store.add(item(run_id, created_at=T0 + timedelta(hours=1)))
    assert again == first and first.status == "open" and first.created_at == T0
    assert store.get(TENANT, first.item_id) == first
    with pytest.raises(ReviewItemNotFoundError):
        store.get(str(uuid.uuid4()), first.item_id)
    with pytest.raises(ReviewItemNotFoundError):
        store.get("not-a-uuid", first.item_id)
    assert store.list(str(uuid.uuid4())) == []
    sampled = store.add(
        item(
            run_id,
            "sampled_low",
            key_id="0123456789abcdef",
            item_id=item_identity(TENANT, DAY, run_id, "s1", "a" * 64, "x"),
        )
    )
    assert sampled.key_id == "0123456789abcdef"
    assert {i.item_id for i in store.list(TENANT, status="open")} >= {first.item_id}


def test_event_and_verdict_are_each_set_once(pg_engine: Any) -> None:
    store = PostgresReviewStore(pg_engine)
    run_id = committed_run(pg_engine)
    added = store.add(item(run_id, "inputs_changed"))
    assert store.set_event(TENANT, added.item_id, 4).event_seq == 4
    assert store.set_event(TENANT, added.item_id, 9).event_seq == 4  # the first event stays
    decided = store.decide(
        TENANT, added.item_id, verdict="missed_risk", decided_by=BOB, decided_at=T0
    )
    assert (decided.status, decided.decided_by, decided.decided_at) == ("missed_risk", BOB, T0)
    with pytest.raises(ReviewAlreadyDecidedError):
        store.decide(TENANT, added.item_id, verdict="confirmed", decided_by=BOB, decided_at=T0)
    assert added.item_id not in {i.item_id for i in store.list(TENANT, status="open")}
    with pytest.raises(ReviewItemNotFoundError):
        store.decide(TENANT, "0" * 64, verdict="confirmed", decided_by=BOB, decided_at=T0)


# --- jobs and queries -----------------------------------------------------------------------------


def test_a_tenant_scoped_review_job_round_trips(pg_engine: Any) -> None:
    jobs = PostgresMaintenanceStore(pg_engine)
    tenant = str(uuid.uuid4())
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (CAST(:t AS uuid), :n)"),
            {"t": tenant, "n": f"review-{tenant[:8]}"},
        )
    job = review_job(tenant, DAY)
    assert jobs.add(job) and not jobs.add(review_job(tenant, DAY))
    assert jobs.get(tenant, job.job_id) == job
    due = [j for j in jobs.due(job.due_at, limit=500) if j.job_id == job.job_id]
    assert due == [job]
    claimed = job.model_copy(update={"revision": 1, "continued": False})
    assert jobs.replace(claimed, expected_revision=0).revision == 1


def test_run_ids_with_a_type_in_a_window_and_tenant_ids(pg_engine: Any) -> None:
    run_id = committed_run(pg_engine)
    audit = AuditLog(PostgresAuditStore(pg_engine), clock=lambda: T0)
    audit.record(run_id, "policy.decided", "test", {"n": 1})
    store = PostgresAuditStore(pg_engine)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert run_id in store.run_ids_with("policy.decided", start, start + timedelta(days=1))
    assert run_id not in store.run_ids_with(
        "policy.decided", start + timedelta(days=1), start + timedelta(days=2)
    )
    assert run_id not in store.run_ids_with("run.created", start, start + timedelta(days=1))
    assert TENANT in PostgresRunStore(pg_engine).tenant_ids()


# --- a whole review -----------------------------------------------------------------------------


class Reviewed(World):
    """The read-back World, with a review key and a rate that draws every candidate."""

    def load(self) -> Any:
        service = assemble(
            contract_review.bundle(self.directory, clm=self.clm),
            stores=postgres_stores(self.engine),
            signer=self.signer,
            provider=FakeProvider([PLAN]),
            background=InlineBackground(),
            clock=self.clock,
            review_key=ReviewKey(bytes(range(100, 132))),
        )
        assert service.maintenance_runner is not None
        handler = next(h for h in service.maintenance_runner._handlers.values() if h.kind == KIND)
        handler._rate = 1.0  # type: ignore[attr-defined]
        return service


def test_a_review_on_postgres_records_items_events_and_verdicts(
    pg_engine: Any, tmp_path: Path
) -> None:
    w = Reviewed(pg_engine, tmp_path)
    assert w.service._orch.advance(w.run_id).status is RunStatus.COMPLETED
    w.clock.now = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    runners = [w.load().maintenance_runner for _ in range(3)]
    start = threading.Barrier(len(runners))
    beats: list[Any] = []
    lock = threading.Lock()

    def go(r: Any) -> None:
        start.wait()
        beat = next(b for b in r.tick() if b.kind == KIND)
        with lock:
            beats.append(beat)

    threads = [threading.Thread(target=go, args=(r,)) for r in runners]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(b.status == "ok" for b in beats)
    job = w.service._maintenance.get(TENANT, review_job(TENANT, DAY).job_id)
    assert job is not None and job.continued and job.result["outcome"] == "reviewed"
    mine = [i for i in PostgresReviewStore(pg_engine).list(TENANT) if i.run_id == w.run_id]
    (sampled,) = mine
    assert sampled.reason == "sampled_low" and sampled.event_seq is not None
    events = PostgresAuditStore(pg_engine).events(w.run_id)
    assert [e.type for e in events].count("review.sampled") == 1

    reviewer = Identity(TENANT, BOB, frozenset({"legal_reviewer"}))
    requester = Identity(TENANT, ALICE, frozenset({"legal_reviewer"}))
    with pytest.raises(ForbiddenError):
        w.service.review_verdict(requester, sampled.item_id, verdict="confirmed")
    decided = w.service.review_verdict(reviewer, sampled.item_id, verdict="confirmed")
    assert decided.status == "confirmed" and decided.decided_by == BOB
    with pytest.raises(ConflictError):
        w.service.review_verdict(reviewer, sampled.item_id, verdict="missed_risk")
