"""Audit log against a real Postgres.

Skipped unless GAWE_TEST_DATABASE_URL is set (`make test-integration` sets it from .env) and
SQLAlchemy + psycopg are installed. Each module run gets its own schema, dropped afterwards, so
the tests never touch real data.
"""

import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")

from sqlalchemy import Engine, text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from gawe.core.audit import AuditLog, ChainConflictError, verify_chain  # noqa: E402
from gawe.core.models import AuditEvent  # noqa: E402
from gawe.db.audit_store import PostgresAuditStore, is_chain_conflict  # noqa: E402
from gawe.db.session import make_engine  # noqa: E402
from gawe.db.sqlscript import load_script, run_script  # noqa: E402

URL = os.environ.get("GAWE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="set GAWE_TEST_DATABASE_URL to run Postgres integration tests"
)

MIGRATION = Path(__file__).parent.parent.parent / "migrations" / "sql" / "0001_init.sql"
TENANT = "00000000-0000-0000-0000-0000000000a1"
USER = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert URL is not None
    schema = f"gawe_it_{uuid.uuid4().hex[:12]}"
    admin = make_engine(URL)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    scoped = make_engine(URL, connect_args={"options": f"-c search_path={schema}"})
    try:
        with scoped.begin() as conn:  # exercises the same helper the Alembic migration uses
            run_script(conn, load_script(MIGRATION))
        with scoped.begin() as conn:
            conn.execute(
                text("INSERT INTO tenants (id, name) VALUES (CAST(:t AS uuid), 'it')"),
                {"t": TENANT},
            )
            conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email) "
                    "VALUES (CAST(:u AS uuid), CAST(:t AS uuid), 'a@it.test')"
                ),
                {"u": USER, "t": TENANT},
            )
        yield scoped
    finally:
        scoped.dispose()
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


@pytest.fixture
def run_id(engine: Engine) -> str:
    rid = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile) "
                "VALUES (CAST(:r AS uuid), CAST(:t AS uuid), CAST(:u AS uuid), 'o', 'p')"
            ),
            {"r": rid, "t": TENANT, "u": USER},
        )
    return rid


def record(engine: Engine, run_id: str, kind: str, actor: str = "alice") -> AuditEvent:
    with engine.begin() as conn:
        return AuditLog(PostgresAuditStore(conn), max_retries=200).record(
            run_id, kind, actor, {"k": kind}
        )


def test_events_round_trip_and_verify(engine: Engine, run_id: str) -> None:
    for kind in ("run.created", "plan.created", "step.executed"):
        record(engine, run_id, kind)
    with engine.connect() as conn:
        events = PostgresAuditStore(conn).events(run_id)
    assert [e.seq for e in events] == [1, 2, 3]
    # timestamps and hashes must survive the database round trip bit-for-bit
    assert verify_chain(events).ok


def test_database_blocks_update_and_delete(engine: Engine, run_id: str) -> None:
    record(engine, run_id, "run.created")
    for statement in ("UPDATE audit_events SET actor = 'mallory'", "DELETE FROM audit_events"):
        with pytest.raises(DBAPIError) as exc, engine.begin() as conn:
            conn.execute(text(statement))
        assert getattr(exc.value.orig, "sqlstate", None) == "42501"


def test_out_of_band_tampering_is_caught_by_verification(engine: Engine, run_id: str) -> None:
    for kind in ("a", "b", "c"):
        record(engine, run_id, kind)
    with engine.begin() as conn:  # an owner who disables the trigger is the threat verify covers
        conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete"))
        conn.execute(
            text(
                "UPDATE audit_events SET actor = 'mallory' "
                "WHERE seq = 2 AND run_id = CAST(:r AS uuid)"
            ),
            {"r": run_id},
        )
        conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete"))
    with engine.connect() as conn:
        result = verify_chain(PostgresAuditStore(conn).events(run_id))
    assert not result.ok and result.failed_seq == 2


def test_concurrent_writers_produce_one_valid_chain(engine: Engine, run_id: str) -> None:
    errors: list[BaseException] = []

    def work(worker: int) -> None:
        try:
            for i in range(8):
                record(engine, run_id, f"w{worker}.{i}", actor=f"w{worker}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    with engine.connect() as conn:
        events = PostgresAuditStore(conn).events(run_id)
    assert [e.seq for e in events] == list(range(1, 33))
    assert verify_chain(events).ok


def test_conflict_does_not_poison_the_callers_transaction(engine: Engine, run_id: str) -> None:
    with engine.begin() as conn:
        store = PostgresAuditStore(conn)
        log = AuditLog(store)
        first = log.record(run_id, "first", "alice")
        stale = AuditEvent(**{**first.model_dump(), "hash": "e" * 64})  # seq 1 again: a fork
        with pytest.raises(ChainConflictError):
            store.append(stale)
        second = log.record(run_id, "second", "alice")  # same transaction keeps working
    assert second.seq == 2
    with engine.connect() as conn:
        assert verify_chain(PostgresAuditStore(conn).events(run_id)).ok


def test_non_conflict_database_errors_propagate(engine: Engine, run_id: str) -> None:
    bad = AuditEvent(
        seq=1,
        run_id=run_id,
        type="t",
        actor="a",
        payload_hash="1" * 64,
        prev_hash="0" * 64,
        hash="not-a-hash",
        ts=datetime.now(UTC),
    )
    with pytest.raises(DBAPIError) as exc, engine.begin() as conn:
        PostgresAuditStore(conn).append(bad)
    assert not is_chain_conflict(exc.value)
    assert getattr(exc.value.orig, "sqlstate", None) == "23514"  # CHECK on hash format
