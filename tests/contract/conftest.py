"""Fixtures that run the same store tests against the in-memory and Postgres implementations.

The Postgres parameter is skipped unless GAWE_TEST_DATABASE_URL is set (`make test-integration`
sets it) and SQLAlchemy + psycopg are installed. Each module gets its own schema, dropped
afterwards, with every migration applied in order.
"""

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gawe.core.approvals import ApprovalStore, InMemoryApprovalStore
from gawe.core.records import ExecutionStore, InMemoryExecutionStore, InMemoryStepStore, StepStore

MIGRATIONS = sorted((Path(__file__).parents[2] / "migrations" / "sql").glob("*.sql"))
TENANT = "00000000-0000-0000-0000-0000000000a1"
ALICE = "00000000-0000-0000-0000-000000000001"
BOB = "00000000-0000-0000-0000-000000000002"
URL = os.environ.get("GAWE_TEST_DATABASE_URL")


@dataclass
class Backend:
    name: str
    steps: StepStore
    approvals: ApprovalStore
    executions: ExecutionStore
    run_id: str


def create_run(conn: Any) -> str:
    """Insert a run whose current plan (version 1) exists; returns the run id."""
    from sqlalchemy import text

    run_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile, "
            "current_plan_version) VALUES (CAST(:r AS uuid), CAST(:t AS uuid), "
            "CAST(:u AS uuid), 'o', 'p', 1)"
        ),
        {"r": run_id, "t": TENANT, "u": ALICE},
    )
    conn.execute(
        text(
            "INSERT INTO plans (run_id, version, steps, planner_model) "
            "VALUES (CAST(:r AS uuid), 1, '[]', 'fake')"
        ),
        {"r": run_id},
    )
    return run_id


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[Any]:
    if not URL:
        pytest.skip("set GAWE_TEST_DATABASE_URL to run Postgres store tests")
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg")
    from sqlalchemy import text

    from gawe.db.session import make_engine
    from gawe.db.sqlscript import load_script, run_script

    schema = f"gawe_store_{uuid.uuid4().hex[:12]}"
    admin = make_engine(URL)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = make_engine(URL, connect_args={"options": f"-c search_path={schema}"})
    try:
        with engine.begin() as conn:
            for path in MIGRATIONS:
                run_script(conn, load_script(path))
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO tenants (id, name) VALUES (CAST(:t AS uuid), 'store-tests')"),
                {"t": TENANT},
            )
            for user, email in ((ALICE, "alice@t.test"), (BOB, "bob@t.test")):
                conn.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, email) "
                        "VALUES (CAST(:u AS uuid), CAST(:t AS uuid), :e)"
                    ),
                    {"u": user, "t": TENANT, "e": email},
                )
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


@pytest.fixture(params=["memory", "postgres"])
def backend(request: pytest.FixtureRequest) -> Iterator[Backend]:
    if request.param == "memory":
        yield Backend(
            "memory",
            InMemoryStepStore(),
            InMemoryApprovalStore(),
            InMemoryExecutionStore(),
            str(uuid.uuid4()),
        )
        return
    engine = request.getfixturevalue("pg_engine")  # skips when Postgres is not configured
    from gawe.db.approval_store import PostgresApprovalStore
    from gawe.db.execution_store import PostgresExecutionStore
    from gawe.db.step_store import PostgresStepStore

    conn = engine.connect()  # one transaction per test, rolled back afterwards
    try:
        run_id = create_run(conn)
        yield Backend(
            "postgres",
            PostgresStepStore(conn),
            PostgresApprovalStore(conn),
            PostgresExecutionStore(conn),
            run_id,
        )
    finally:
        conn.rollback()
        conn.close()
