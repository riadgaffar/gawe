"""M24 S1: history and subject locks over Postgres, end to end with the test app."""

import threading
import time
import uuid
from typing import Any

from sqlalchemy import text

from gawe.api.auth import Identity
from gawe.api.wiring import postgres_stores
from gawe.core.history import HistoryEntry
from gawe.db.history_store import PostgresSubjectLocks
from tests.contract.conftest import ALICE, BOB, TENANT
from tests.unit.test_history import App, assess

CASE = Identity(TENANT, ALICE, frozenset({"case_manager"}))
LEGAL = Identity(TENANT, BOB, frozenset({"legal_reviewer"}))


def test_history_counts_this_tenants_earlier_assessment(pg_engine: Any) -> None:
    app = App([assess("PG-1"), assess("PG-1")], postgres_stores(pg_engine))
    assert app.run(CASE).status == "completed"
    second = app.run(CASE)
    assert second.status == "paused_for_approval"
    assert "repeat-assessment-needs-legal" in second.steps[-1].policy_rule_ids
    (item,) = app.service.inbox(LEGAL)
    app.service.decide(LEGAL, item.approval_id, decision="approved", action_hash=item.action_hash)
    assert app.service.get_run(CASE, second.run_id).status == "completed"


def test_another_tenants_history_doesnt_count(pg_engine: Any) -> None:
    other_tenant, other_user = str(uuid.uuid4()), str(uuid.uuid4())
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (CAST(:t AS uuid), 'other')"),
            {"t": other_tenant},
        )
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email) "
                "VALUES (CAST(:u AS uuid), CAST(:t AS uuid), 'carol@o.test')"
            ),
            {"u": other_user, "t": other_tenant},
        )
    other = Identity(other_tenant, other_user, frozenset({"case_manager"}))
    app = App([assess("PG-2"), assess("PG-2")], postgres_stores(pg_engine))
    assert app.run(CASE).status == "completed"
    assert app.run(other).status == "completed"


def test_two_runs_at_once_dont_both_see_nothing_before(pg_engine: Any) -> None:
    stores = postgres_stores(pg_engine)
    reader = stores.history
    assert reader is not None

    class Slow:
        def earlier(self, *args: Any) -> list[HistoryEntry]:
            found = reader.earlier(*args)
            time.sleep(0.3)
            return found

    from dataclasses import replace

    app = App([assess("PG-3"), assess("PG-3")], replace(stores, history=Slow()))
    views: list[Any] = []
    threads = [threading.Thread(target=lambda: views.append(app.run(CASE))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(v.status for v in views) == ["completed", "paused_for_approval"]


def test_the_advisory_lock_serializes_one_key_only(pg_engine: Any) -> None:
    locks = PostgresSubjectLocks(pg_engine)
    order: list[str] = []

    def hold(key: str, name: str) -> None:
        with locks.hold(key):
            order.append(f"{name}+")
            time.sleep(0.2)
            order.append(f"{name}-")

    first = threading.Thread(target=hold, args=("k1", "a"))
    first.start()
    time.sleep(0.05)
    other_key = threading.Thread(target=hold, args=("k2", "c"))
    same_key = threading.Thread(target=hold, args=("k1", "b"))
    other_key.start()
    same_key.start()
    for t in (first, other_key, same_key):
        t.join()
    assert order.index("b+") > order.index("a-")  # same key waits
    assert order.index("c+") < order.index("a-")  # another key doesn't
