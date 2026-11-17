"""Scheduled runners over real per-call Postgres commits: competition, crashes and backlog."""

import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, postgres_stores
from gawe.apps import it_access
from gawe.connectors.base import Ledger
from gawe.connectors.mock_iam import MockIAM
from gawe.connectors.mock_tickets import MockTickets
from gawe.core.audit import verify_chain
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.llm.fake import FakeProvider
from gawe.maintenance.runner import Heartbeat, MaintenanceRunner
from tests.contract.conftest import ALICE, TENANT
from tests.contract.test_readback_postgres import World
from tests.flow import Clock, Crash
from tests.helpers import signer


def runner(service: Any) -> MaintenanceRunner:
    assert service.maintenance_runner is not None
    return service.maintenance_runner


def mine(beats: list[Heartbeat]) -> Heartbeat:
    """The read-back heartbeat; the daily review reports alongside it."""
    (beat,) = [b for b in beats if b.kind == "write_readback"]
    return beat


def test_independent_runner_processes_resolve_one_operation_once(
    pg_engine: Any, tmp_path: Path
) -> None:
    w = World(pg_engine, tmp_path)
    w.clm.operations.visible_at = w.clock.now + timedelta(seconds=60)
    assert w.service._orch.advance(w.run_id).status is RunStatus.WAITING_VERIFICATION
    w.clock.advance(seconds=60)
    runners = [runner(w.load()) for _ in range(3)]
    ready = threading.Barrier(len(runners))
    beats: list[Heartbeat] = []
    lock = threading.Lock()

    def go(r: MaintenanceRunner) -> None:
        ready.wait()
        beat = mine(r.tick())
        with lock:
            beats.append(beat)

    threads = [threading.Thread(target=go, args=(r,)) for r in runners]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(b.status == "ok" for b in beats)
    assert sum(b.outcomes.get("done", 0) for b in beats) == 1
    assert w.service._runs.get(w.run_id).status is RunStatus.COMPLETED
    assert w.job.result["outcome"] == "agreement" and w.job.continued
    assert len(w.calls) == 1 and len(w.clm.operations.records()) == 1


def test_runner_crash_after_committed_result_is_finished_by_a_new_process(
    pg_engine: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(pg_engine, tmp_path)
    w.clm.operations.visible_at = w.clock.now + timedelta(seconds=60)
    assert w.service._orch.advance(w.run_id).status is RunStatus.WAITING_VERIFICATION
    w.clock.advance(seconds=60)
    resolver = w.service._orch._executor.verification
    assert resolver is not None
    original = resolver.steps.transition

    def after_step(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, **kwargs)
        if kwargs.get("to") is StepStatus.SUCCEEDED:
            raise Crash()
        return value

    monkeypatch.setattr(resolver.steps, "transition", after_step)
    with pytest.raises(Crash):
        runner(w.service).tick()
    monkeypatch.undo()
    assert w.job.result is not None and not w.job.continued
    w.clock.advance(seconds=31)
    w.service = w.load()  # entirely new stores, adapters, orchestrator and runner
    beat = mine(runner(w.service).tick())
    assert beat.status == "ok" and beat.outcomes["continued"] == 1
    assert w.service._runs.get(w.run_id).status is RunStatus.COMPLETED
    assert w.job.continued and w.job.evidence_seq is not None and len(w.calls) == 1


def test_deadline_unknown_is_persisted_and_retained_in_postgres_backlog(
    pg_engine: Any, tmp_path: Path
) -> None:
    w = World(pg_engine, tmp_path)
    w.clm.operations.visible_at = w.clock.now + timedelta(minutes=10)
    assert w.service._orch.advance(w.run_id).status is RunStatus.WAITING_VERIFICATION
    before = mine(runner(w.service).tick())
    w.clock.now = w.job.deadline
    after = mine(runner(w.load()).tick())
    assert after.outcomes["newly_unknown"] == 1
    assert after.unknown_total == (before.unknown_total or 0) + 1
    assert w.service._runs.get(w.run_id).status is RunStatus.UNKNOWN
    assert w.job.result["outcome"] == "unknown" and w.job.continued and len(w.calls) == 1
    later = mine(runner(w.load()).tick())
    assert later.unknown_total == after.unknown_total  # retained after acknowledgment


# -- grant expiry (M16b) -----------------------------------------------------------------------


class Grants:
    """IT access over real Postgres stores: one grant, then sweeps at its end date."""

    def __init__(self, engine: Any, directory: Path) -> None:
        self.engine, self.directory, self.clock = engine, directory, Clock()
        self.iam = MockIAM.from_seed(
            it_access.HERE / "samples/iam_seed.json", Ledger(directory / "ledger.jsonl")
        )
        self.tickets = MockTickets.in_state_dir(directory)
        self.run_id, self.tenant = str(uuid4()), TENANT
        self.service = self.load(
            [
                {
                    "steps": [
                        {
                            "id": "grant",
                            "tool": "iam.grant_access",
                            "depends_on": [],
                            "args": {
                                "email": "dana.ortiz@acme.example",
                                "system": "wiki",
                                "level": "read",
                            },
                        }
                    ]
                }
            ]
        )
        self.tickets.create(
            TENANT, "T-1", submitted_by="dana.ortiz@acme.example", channel="portal", body="t"
        )
        self.service._orch.start_run(
            run_id=self.run_id,
            tenant_id=TENANT,
            acting_user_id=ALICE,
            roles=("it_agent",),
            objective="Grant access",
            profile="it_access",
            source_id="T-1",
        )

    def load(self, script: list[Any] | None = None) -> Any:
        bundle = replace(
            it_access.bundle(self.directory, iam=self.iam, tickets=self.tickets, clock=self.clock),
            variables=lambda run: {
                "tenant": {"internal_domains": ["acme.example"], "max_grant_lifetime_hours": 720},
                "request": {
                    "reported_requester": "dana.ortiz@acme.example",
                    "submitters": ["dana.ortiz@acme.example"],
                    "portal_submitters": ["dana.ortiz@acme.example"],
                },
            },
        )
        return assemble(
            bundle,
            stores=postgres_stores(self.engine),
            signer=signer(),
            provider=FakeProvider(script or []),
            background=InlineBackground(),
            clock=self.clock,
        )

    def job(self) -> Any:
        (job,) = [
            j
            for j in self.service._maintenance.for_run(self.tenant, self.run_id)
            if j.kind == "grant_expiry"
        ]
        return job

    def revoked_events(self) -> int:
        events = self.service._orch._audit.events(self.run_id)
        return sum(e.type == "grant.revoked" for e in events)


def expiry(beats: list[Heartbeat]) -> Heartbeat:
    (beat,) = [b for b in beats if b.kind == "grant_expiry"]
    return beat


def test_competing_runner_processes_revoke_an_expired_grant_once(
    pg_engine: Any, tmp_path: Path
) -> None:
    g = Grants(pg_engine, tmp_path)
    assert g.service._orch.advance(g.run_id).status is RunStatus.COMPLETED
    g.clock.now = g.job().due_at
    runners = [runner(g.load()) for _ in range(3)]
    ready = threading.Barrier(len(runners))
    beats: list[Heartbeat] = []
    lock = threading.Lock()

    def go(r: MaintenanceRunner) -> None:
        ready.wait()
        beat = expiry(r.tick())
        with lock:
            beats.append(beat)

    threads = [threading.Thread(target=go, args=(r,)) for r in runners]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(b.status == "ok" for b in beats)
    assert sum(b.outcomes.get("revoked", 0) for b in beats) == 1
    assert g.job().continued and g.job().result["outcome"] == "revoked"
    assert len(g.iam.revocations()) == 1 and g.revoked_events() == 1

    assert verify_chain(g.service._orch._audit.events(g.run_id)).ok


def test_a_crash_after_revoking_is_finished_once_by_a_new_process(
    pg_engine: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = Grants(pg_engine, tmp_path)
    g.service._orch.advance(g.run_id)
    g.clock.now = g.job().due_at
    original = g.iam.grant_state

    def crash_after_revoke(*args: Any) -> Any:
        raise Crash()  # the revoke landed; the process died before reading it back

    monkeypatch.setattr(g.iam, "grant_state", crash_after_revoke)
    with pytest.raises(Crash):
        runner(g.service).tick()
    monkeypatch.setattr(g.iam, "grant_state", original)
    assert len(g.iam.revocations()) == 1 and g.job().result is None
    g.clock.advance(seconds=31)  # the dead process's lease runs out
    beat = expiry(runner(g.load()).tick())
    assert beat.outcomes["revoked"] == 1 and beat.overdue_active == 0
    assert len(g.iam.revocations()) == 1 and g.revoked_events() == 1
