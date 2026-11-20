"""Real per-call commits: crash recovery and independent workers resolving one write."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, postgres_stores
from gawe.apps import contract_review
from gawe.connectors.base import Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.core.executor import ToolCall
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.llm.fake import FakeProvider
from tests.contract.conftest import ALICE, TENANT
from tests.flow import Clock, Crash
from tests.helpers import signer

PLAN = {
    "steps": [
        {
            "id": "write",
            "tool": "crm.write_assessment",
            "depends_on": [],
            # Not "low": since M17 every "low" waits for legal; these need an automatic write.
            "args": {"contract_id": "C-1001", "risk_level": "medium", "summary": "Review"},
        }
    ]
}


class World:
    def __init__(self, engine: Any, directory: Path) -> None:
        self.engine, self.directory, self.clock = engine, directory, Clock()
        self.signer = signer()
        self.clm = MockCLM.from_seed(
            contract_review.HERE / "samples/clm_seed.json", Ledger(directory / "ledger.jsonl")
        )
        self.calls: list[ToolCall] = []
        self.run_id = str(uuid4())
        self.service = self.load()
        self.service._orch.start_run(
            run_id=self.run_id,
            tenant_id=TENANT,
            acting_user_id=ALICE,
            roles=("case_manager",),
            objective="Review",
            profile="contract_review",
        )

    def load(self) -> Any:
        bundle = contract_review.bundle(self.directory, clm=self.clm)

        def write(args: Any, call: ToolCall) -> Any:
            self.calls.append(call)
            return self.clm.write_assessment(
                call.idempotency_key,
                args["contract_id"],
                args["risk_level"],
                args["summary"],
                contract_version=args["record"]["contract_version"],
            )

        bundle = replace(bundle, tools={**bundle.tools, "crm.write_assessment": write})
        return assemble(
            bundle,
            stores=postgres_stores(self.engine),
            signer=self.signer,
            provider=FakeProvider([PLAN]),
            background=InlineBackground(),
            clock=self.clock,
        )

    @property
    def job(self) -> Any:
        return self.service._maintenance.for_run(TENANT, self.run_id)[0]


@pytest.mark.parametrize(
    "boundary", ["intent", "accept", "result", "step", "audit", "continuation"]
)
def test_committed_crash_boundaries_recover_without_another_write(
    pg_engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    w = World(pg_engine, tmp_path)
    resolver = w.service._orch._executor.verification
    assert resolver is not None
    if boundary == "accept":
        target, name = w.clm.operations, "accept"
    elif boundary == "intent":
        target, name = resolver.jobs, "add"
    elif boundary == "result":
        target, name = resolver.jobs, "replace"
    elif boundary == "step":
        target, name = resolver.steps, "transition"
    elif boundary == "audit":
        target, name = resolver.audit, "record"
    else:
        target, name = resolver, "acknowledge"
    original = getattr(target, name)

    def fail(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        should_fail = (
            boundary in {"intent", "accept", "continuation"}
            or (boundary == "result" and args[0].result is not None)
            or (boundary == "step" and kwargs.get("to") is StepStatus.SUCCEEDED)
            or (boundary == "audit" and args[1] == "verification.readback")
        )
        if should_fail:
            raise Crash()
        return result

    monkeypatch.setattr(target, name, fail)
    with pytest.raises(Crash):
        w.service._orch.advance(w.run_id)
    deadline, call_count = w.job.deadline, len(w.calls)
    monkeypatch.undo()
    w.clock.advance(seconds=31)
    w.service = w.load()  # entirely new stores, closures, adapters and orchestrator
    result = w.service._orch.advance(w.run_id)
    if boundary == "intent":
        assert result.status is RunStatus.WAITING_VERIFICATION
        w.clock.advance(minutes=5)
        assert w.service._orch.advance(w.run_id).status is RunStatus.UNKNOWN
    else:
        assert result.status is RunStatus.COMPLETED
    assert w.job.deadline == deadline and w.job.continued
    assert len(w.calls) == call_count
    assert len(w.clm.operations.records()) == (0 if boundary == "intent" else 1)


def test_two_independent_drivers_resolve_one_durable_operation(
    pg_engine: Any, tmp_path: Path
) -> None:
    w = World(pg_engine, tmp_path)
    w.clm.operations.visible_at = w.clock.now + timedelta(seconds=60)
    assert w.service._orch.advance(w.run_id).status is RunStatus.WAITING_VERIFICATION
    w.clock.advance(seconds=60)
    services = [w.load(), w.load()]
    import threading

    ready = threading.Barrier(2)

    def resolve(service: Any) -> Any:
        ready.wait()
        return service._orch.advance(w.run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resolve, services))
    assert all(r.status in {RunStatus.WAITING_VERIFICATION, RunStatus.COMPLETED} for r in results)
    assert w.service._runs.get(w.run_id).status is RunStatus.COMPLETED
    assert w.job.result["outcome"] == "agreement" and w.job.continued
    assert len(w.calls) == 1 and len(w.clm.operations.records()) == 1


def test_stale_dispatcher_cannot_retry_after_independent_worker_cancels(
    pg_engine: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(pg_engine, tmp_path)
    other = w.load()
    w.clm.faults.fail_next()

    def takeover(_: float) -> None:
        w.clock.advance(seconds=31)
        step = other._steps.get(w.run_id, "write")
        other._orch._executor.reconcile(step)
        other._orch.cancel(w.run_id, by=ALICE)

    monkeypatch.setattr(w.service._orch._executor, "_sleep", takeover)
    assert w.service._orch.advance(w.run_id).status is RunStatus.CANCELLED
    assert len(w.calls) == 1
    assert w.clm.operations.records() == []
