"""Races and durability against a real Postgres. Each worker uses its own transaction.

Skipped unless GAWE_TEST_DATABASE_URL is set (see conftest). Stores here are bound to the Engine,
so every store call commits on its own, exactly as the executor expects in production.
"""

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")

from sqlalchemy import text  # noqa: E402

from gawe.core.approvals import (  # noqa: E402
    ApprovalAlreadyDecidedError,
    ApprovalAlreadyOpenError,
    ApprovalRecord,
    ApprovalService,
)
from gawe.core.audit import AuditLog, verify_chain  # noqa: E402
from gawe.core.canonical import action_hash  # noqa: E402
from gawe.core.executor import (  # noqa: E402
    ConcurrentExecutionError,
    ExecutionRefusedError,
    Executor,
    RetryPolicy,
)
from gawe.core.records import AttemptConflictError, StaleStateError, StepRecord  # noqa: E402
from gawe.core.registry import ToolRegistry  # noqa: E402
from gawe.core.statemachine import StepStatus  # noqa: E402
from gawe.core.verifier import Verifier  # noqa: E402
from gawe.db.approval_store import PostgresApprovalStore  # noqa: E402
from gawe.db.audit_store import PostgresAuditStore  # noqa: E402
from gawe.db.execution_store import PostgresExecutionStore  # noqa: E402
from gawe.db.step_store import PostgresStepStore  # noqa: E402
from tests.contract.conftest import ALICE, BOB, create_run  # noqa: E402
from tests.flow import Clock, Crash, FakeConnector, manifests  # noqa: E402
from tests.helpers import TOOLS, signer  # noqa: E402

H = "a" * 64
TOOL = "contracts.send_email"
ARGS = {"to": "colleague@acme.com", "body": "hello"}
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def race(workers: int, job: Callable[[int], Any]) -> list[Any]:
    """Run `job` in parallel threads released together; return each result or exception."""
    start = threading.Barrier(workers)
    results: list[Any] = [None] * workers

    def work(n: int) -> None:
        start.wait()
        try:
            results[n] = job(n)
        except BaseException as exc:  # noqa: BLE001
            results[n] = exc

    threads = [threading.Thread(target=work, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def committed_run(engine: Any) -> str:
    with engine.begin() as conn:
        return create_run(conn)


def step(run_id: str, status: StepStatus = StepStatus.APPROVED, **kw: Any) -> StepRecord:
    fields: dict[str, Any] = {
        "run_id": run_id,
        "step_id": "s1",
        "tool": TOOL,
        "tool_version": "1.0.0",
        "status": status,
        "action_hash": H,
    }
    return StepRecord(**{**fields, **kw})


def test_exactly_one_worker_wins_a_step_transition(pg_engine: Any) -> None:
    run_id = committed_run(pg_engine)
    PostgresStepStore(pg_engine).add(step(run_id))

    def claim(_: int) -> StepRecord:
        return PostgresStepStore(pg_engine).transition(
            run_id, "s1", expected=StepStatus.APPROVED, to=StepStatus.EXECUTING
        )

    results = race(8, claim)
    assert sum(isinstance(r, StepRecord) for r in results) == 1
    assert all(isinstance(r, StaleStateError) for r in results if not isinstance(r, StepRecord))


def test_exactly_one_open_approval_per_step_under_a_race(pg_engine: Any) -> None:
    run_id = committed_run(pg_engine)
    PostgresStepStore(pg_engine).add(step(run_id, StepStatus.AWAITING_APPROVAL))

    def request(_: int) -> None:
        PostgresApprovalStore(pg_engine).add(
            ApprovalRecord(
                approval_id=str(uuid.uuid4()),
                run_id=run_id,
                step_id="s1",
                action_hash=H,
                approver_role="legal_reviewer",
                requested_by=ALICE,
                requested_at=T0,
                expires_at=T0 + timedelta(hours=1),
            )
        )

    results = race(6, request)
    assert sum(r is None for r in results) == 1
    assert all(isinstance(r, ApprovalAlreadyOpenError) for r in results if r is not None)


def test_exactly_one_decision_wins(pg_engine: Any) -> None:
    run_id = committed_run(pg_engine)
    PostgresStepStore(pg_engine).add(step(run_id, StepStatus.AWAITING_APPROVAL))
    approval_id = str(uuid.uuid4())
    PostgresApprovalStore(pg_engine).add(
        ApprovalRecord(
            approval_id=approval_id,
            run_id=run_id,
            step_id="s1",
            action_hash=H,
            approver_role="legal_reviewer",
            requested_by=ALICE,
            requested_at=T0,
            expires_at=T0 + timedelta(hours=1),
        )
    )

    def decide(_: int) -> ApprovalRecord:
        return PostgresApprovalStore(pg_engine).record_decision(
            approval_id, decided_by=BOB, decision="approved", comment=None, decided_at=T0
        )

    results = race(6, decide)
    assert sum(isinstance(r, ApprovalRecord) for r in results) == 1
    assert all(
        isinstance(r, ApprovalAlreadyDecidedError)
        for r in results
        if not isinstance(r, ApprovalRecord)
    )


def test_exactly_one_worker_starts_an_attempt(pg_engine: Any) -> None:
    run_id = committed_run(pg_engine)
    PostgresStepStore(pg_engine).add(step(run_id, StepStatus.EXECUTING))

    def begin(_: int) -> Any:
        return PostgresExecutionStore(pg_engine).start(
            run_id, "s1", attempt=1, idempotency_key="k", now=T0
        )

    results = race(6, begin)
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    assert all(isinstance(r, AttemptConflictError) for r in results if isinstance(r, BaseException))
    assert len(PostgresExecutionStore(pg_engine).attempts(run_id, "s1")) == 1


def test_a_replan_does_not_inherit_approvals_attempts_or_output(pg_engine: Any) -> None:
    run_id = committed_run(pg_engine)
    steps = PostgresStepStore(pg_engine)
    steps.add(step(run_id, StepStatus.EXECUTING))
    approval_id = str(uuid.uuid4())
    PostgresApprovalStore(pg_engine).add(
        ApprovalRecord(
            approval_id=approval_id,
            run_id=run_id,
            step_id="s1",
            action_hash=H,
            approver_role="legal_reviewer",
            requested_by=ALICE,
            requested_at=T0,
            expires_at=T0 + timedelta(hours=1),
        )
    )
    executions = PostgresExecutionStore(pg_engine)
    executions.start(run_id, "s1", attempt=1, idempotency_key="k", now=T0)
    executions.save_output(run_id, "s1", {"message_id": "m1"})

    with pg_engine.begin() as conn:  # version 2 of the plan becomes current
        conn.execute(
            text(
                "INSERT INTO plans (run_id, version, steps, planner_model) "
                "VALUES (CAST(:r AS uuid), 2, '[]', 'fake')"
            ),
            {"r": run_id},
        )
        conn.execute(
            text("UPDATE runs SET current_plan_version = 2 WHERE id = CAST(:r AS uuid)"),
            {"r": run_id},
        )
    steps.add(step(run_id, StepStatus.READY, action_hash=None))

    assert steps.get(run_id, "s1").status is StepStatus.READY
    assert PostgresApprovalStore(pg_engine).for_step(run_id, "s1") == []
    assert executions.attempts(run_id, "s1") == []
    assert executions.load_output(run_id, "s1") is None
    # the old plan's records still exist, just unreachable from the new step
    assert PostgresApprovalStore(pg_engine).get(approval_id).is_open


# ------------------------------------------------------------------ executor on Postgres


def build_executor(
    engine: Any, connector: FakeConnector, clock: Clock, lease: timedelta
) -> Executor:
    audit = AuditLog(PostgresAuditStore(engine), max_retries=200)
    registry = ToolRegistry()
    for manifest in manifests().values():
        registry.register(manifest)
    return Executor(
        steps=PostgresStepStore(engine),
        executions=PostgresExecutionStore(engine),
        registry=registry,
        tools={name: connector for name in TOOLS},
        signer=signer(),
        approvals=ApprovalService(PostgresApprovalStore(engine), signer(), audit),
        audit=audit,
        verifier=Verifier(),
        retry=RetryPolicy(max_attempts=3),
        lease=lease,
        clock=clock,
        sleep=lambda _: None,
    )


def ready_step(engine: Any) -> tuple[str, Any]:
    run_id = committed_run(engine)
    digest = action_hash(
        tool=TOOL, version="1.0.0", resolved_args=ARGS, run_id=run_id, step_id="s1"
    )
    PostgresStepStore(engine).add(
        step(run_id, StepStatus.POLICY_CHECKED, action_hash=digest, resolved_args=ARGS)
    )
    decision = signer().seal(
        step_id="s1", action_hash=digest, outcome="allow", rule_ids=["t"], reason="test"
    )
    return run_id, decision


def test_executor_runs_the_tool_once_when_workers_race(pg_engine: Any) -> None:
    run_id, decision = ready_step(pg_engine)
    connector, clock = FakeConnector(), Clock()

    def work(_: int) -> Any:
        return build_executor(pg_engine, connector, clock, timedelta(minutes=5)).execute(
            run_id, "s1", decision
        )

    results = race(8, work)
    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1 and winners[0].status is StepStatus.SUCCEEDED
    assert all(isinstance(r, ExecutionRefusedError) for r in losers), losers
    assert len(connector.calls) == 1
    assert PostgresStepStore(pg_engine).get(run_id, "s1").status is StepStatus.SUCCEEDED
    events = PostgresAuditStore(pg_engine).events(run_id)
    assert verify_chain(events).ok


def test_executor_state_survives_a_crash_and_resumes_once(pg_engine: Any) -> None:
    run_id, decision = ready_step(pg_engine)
    connector, clock = FakeConnector(), Clock()
    connector.script = [Crash()]
    executor = build_executor(pg_engine, connector, clock, timedelta(minutes=5))

    with pytest.raises(Crash):
        executor.execute(run_id, "s1", decision)

    # the claim and the started attempt were committed before the tool ran
    assert PostgresStepStore(pg_engine).get(run_id, "s1").status is StepStatus.EXECUTING
    attempts = PostgresExecutionStore(pg_engine).attempts(run_id, "s1")
    assert [a.status for a in attempts] == ["started"]

    other = build_executor(pg_engine, connector, clock, timedelta(minutes=5))
    with pytest.raises(ConcurrentExecutionError):
        other.execute(run_id, "s1", decision)  # the first worker might still be alive

    clock.advance(minutes=6)
    outcome = other.execute(run_id, "s1", decision)
    assert outcome.status is StepStatus.SUCCEEDED and outcome.attempts == 2
    assert len({c.idempotency_key for c in connector.calls}) == 1
    assert len(connector.effects) == 1
    assert verify_chain(PostgresAuditStore(pg_engine).events(run_id)).ok
