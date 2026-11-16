"""Scheduled verification: ownership, crash windows, fairness, heartbeats and the alarm rules.

Every test drives `MaintenanceRunner.tick` with an injected clock; none issues a request or
advances a run itself unless the scenario is about a competing driver.
"""

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.flow import T0, Clock, Crash
from tests.helpers import KEY, signer
from tests.unit.test_write_readback import ALICE, CRM, MAIL, Env

from gawe.api.app import create_app
from gawe.api.auth import TokenAuthenticator
from gawe.api.background import InlineBackground
from gawe.api.service import ApiService
from gawe.api.wiring import assemble
from gawe.core.maintenance import InMemoryMaintenanceStore, MaintenanceJob
from gawe.core.readback import ReadbackResult
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.core.telemetry import InMemoryTelemetry
from gawe.llm.fake import FakeProvider
from gawe.maintenance import __main__ as cli
from gawe.maintenance.readback import ReadbackJobs
from gawe.maintenance.runner import Heartbeat, MaintenanceRunner, assess
from gawe.maintenance.schedule import PeriodicTicker
from gawe.maintenance.settings import MaintenanceSettings, SettingsError

EXTERNAL = {**MAIL, "to": "vendor@outside.example"}


def restart(
    e: Env,
    provider: FakeProvider | None = None,
    background: Any = None,
    **kwargs: Any,
) -> ApiService:
    """A new process: new resolver, orchestrator and runner over the same durable stores."""
    return assemble(
        e.bundle,
        stores=e.stores,
        signer=signer(),
        provider=provider or FakeProvider([]),
        background=background or InlineBackground(),
        clock=e.clock,
        **kwargs,
    )


def runner(service: ApiService) -> MaintenanceRunner:
    assert service.maintenance_runner is not None
    return service.maintenance_runner


def only(beats: list[Heartbeat]) -> Heartbeat:
    """The read-back heartbeat. IT access also runs grant expiry, which has its own."""
    assert len(beats) == len({b.kind for b in beats})
    (beat,) = [b for b in beats if b.kind == "write_readback"]
    return beat


def status(e: Env, step: str = "write") -> tuple[RunStatus, StepStatus]:
    return e.stores.runs.get(e.run_id).status, e.stores.steps.get(e.run_id, step).status


# -- resolution without requests ------------------------------------------------------------


@pytest.mark.parametrize("system", ["crm", "iam", "mail", "esign"])
def test_delayed_outcome_resolves_and_continues_with_no_request(
    tmp_path: Path, system: str
) -> None:
    e = Env(tmp_path, system, downstream=system != "iam")
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    sweep = runner(e.service)
    e.clock.advance(seconds=30)
    beat = only(sweep.tick())
    assert beat.outcomes["still_waiting"] == 1 and beat.unresolved_total == 1
    assert status(e) == (RunStatus.WAITING_VERIFICATION, StepStatus.AWAITING_READBACK)
    e.clock.advance(seconds=30)
    beat = only(sweep.tick())
    assert beat.status == "ok" and beat.checked == 1 and beat.outcomes["done"] == 1
    assert status(e) == (RunStatus.COMPLETED, StepStatus.SUCCEEDED)
    if system != "iam":
        assert e.stores.steps.get(e.run_id, "next").status is StepStatus.SUCCEEDED
    assert e.job.continued and len(e.calls) == 1
    assert only(sweep.tick()).checked == 0  # acknowledged work leaves the queue


def test_disagreement_is_blocked_and_counted(tmp_path: Path) -> None:
    e = Env(tmp_path, "mail")
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.target.accepted_override = ("legal@acme.example", "leak@outside.example")
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    e.clock.advance(seconds=60)
    beat = only(runner(e.service).tick())
    assert beat.outcomes["blocked"] == 1
    assert status(e) == (RunStatus.FAILED, StepStatus.VERIFICATION_BLOCKED)
    assert len(e.calls) == 1 and len(e.target.records()) == 1  # detected, not undone


def test_deadline_expiry_is_persisted_and_unknown_stays_in_backlog(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.target.visible_at = e.clock.now + timedelta(minutes=10)
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    deadline = e.job.deadline
    sweep = runner(e.service)
    e.clock.now = deadline
    beat = only(sweep.tick())
    assert beat.outcomes["newly_unknown"] == 1 and beat.unknown_total == 1
    assert status(e) == (RunStatus.UNKNOWN, StepStatus.VERIFICATION_UNKNOWN)
    assert e.job.result == {"outcome": "unknown", "code": "verification_deadline"}
    assert e.job.deadline == deadline and e.job.continued
    e.clock.advance(minutes=10)  # now visible, but a terminal unknown is never rewritten
    beat = only(sweep.tick())
    assert beat.checked == 0 and beat.unknown_total == 1 and beat.unresolved_total == 0
    assert status(e)[0] is RunStatus.UNKNOWN and len(e.calls) == 1


def test_zero_work_tick_still_emits_a_successful_heartbeat(tmp_path: Path) -> None:
    beat = only(runner(Env(tmp_path).service).tick())
    assert beat.status == "ok" and beat.checked == 0
    assert beat.unresolved_total == 0 and beat.unknown_total == 0
    assert set(beat.fields()["outcomes"]) >= {"done", "blocked", "newly_unknown", "still_waiting"}


def test_cancelled_run_is_reconciled_without_resuming(tmp_path: Path) -> None:
    e = Env(tmp_path, downstream=True)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    e.service.cancel_run(ALICE, e.run_id)
    e.clock.advance(seconds=60)
    beat = only(runner(e.service).tick())
    assert beat.outcomes["done"] == 1
    assert e.job.result["outcome"] == "agreement" and e.job.continued
    assert e.stores.runs.get(e.run_id).status is RunStatus.CANCELLED
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.FAILED
    assert len(e.calls) == 1


class DeferredBackground:
    """Holds submitted work until the test runs it: a continuation still in progress."""

    def __init__(self) -> None:
        self.jobs: list[Any] = []

    def submit(self, job: Any) -> None:
        self.jobs.append(job)

    def run_all(self) -> None:
        jobs, self.jobs = self.jobs, []
        for job in jobs:
            job()


def test_api_continuation_runs_off_the_sweep_thread(tmp_path: Path) -> None:
    e = Env(tmp_path, downstream=True)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    pool = DeferredBackground()
    sweep = runner(restart(e, background=pool))
    e.clock.advance(seconds=60)
    beat = only(sweep.tick())  # returns while the next step has not run
    assert beat.status == "ok" and beat.outcomes["done"] == 1 and len(pool.jobs) == 1
    assert e.stores.steps.get(e.run_id, "write").status is StepStatus.SUCCEEDED
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.PENDING
    assert not e.job.continued and e.job.due_at == e.clock.now + timedelta(seconds=30)
    assert only(sweep.tick()).checked == 0  # moved back one interval; not handed off again
    e.clock.advance(seconds=30)
    assert only(sweep.tick()).outcomes["pending_continuation"] == 1
    pool.run_all()  # two hand-offs of one run; the second finds nothing left to do
    assert status(e, "next") == (RunStatus.COMPLETED, StepStatus.SUCCEEDED)
    assert e.job.continued and len(e.calls) == 1
    e.clock.advance(seconds=30)
    assert only(sweep.tick()).checked == 0


# -- ownership: competing runners, leases, stale completion ----------------------------------


def test_competing_runner_with_a_stale_page_loses_the_claim(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    e.clock.advance(seconds=60)
    stale_page = e.stores.maintenance.due(e.clock.now)
    a, b = runner(e.service), runner(restart(e))
    assert only(a.tick()).outcomes["done"] == 1

    class StalePage:
        def __init__(self, store: Any) -> None:
            self._store, self._served = store, False

        def due(self, *args: Any, **kwargs: Any) -> list[MaintenanceJob]:
            if self._served:
                return []
            self._served = True
            return stale_page

        def __getattr__(self, name: str) -> Any:
            return getattr(self._store, name)

    b._store = StalePage(e.stores.maintenance)  # type: ignore[assignment]
    beat = only(b.tick())
    assert beat.checked == 1 and beat.contended == 1 and beat.outcomes.get("done", 0) == 0
    assert len(e.calls) == 1 and status(e)[0] is RunStatus.COMPLETED


def test_competing_runner_threads_converge_on_one_resolution(tmp_path: Path) -> None:
    e = Env(tmp_path, downstream=True)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    e.clock.advance(seconds=60)
    runners = [runner(restart(e)) for _ in range(4)]
    ready = threading.Barrier(len(runners))
    results: list[Heartbeat] = []

    def go(r: MaintenanceRunner) -> None:
        ready.wait()
        results.append(only(r.tick()))

    threads = [threading.Thread(target=go, args=(r,)) for r in runners]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(b.outcomes.get("done", 0) for b in results) == 1
    assert status(e) == (RunStatus.COMPLETED, StepStatus.SUCCEEDED)
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.SUCCEEDED
    assert len(e.calls) == 1 and e.job.continued


def test_runner_that_dies_holding_a_lease_is_taken_over_after_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    e.clock.advance(seconds=60)
    a = runner(e.service)

    def die(job: MaintenanceJob) -> str:
        raise Crash()

    monkeypatch.setattr(a._handlers["write_readback"], "process", die)
    with pytest.raises(Crash):
        a.tick()
    assert e.job.lease_owner is not None and e.job.result is None
    b = runner(restart(e))
    e.clock.advance(seconds=10)
    assert only(b.tick()).checked == 0  # the lease is still live
    e.clock.advance(seconds=21)
    assert only(b.tick()).outcomes["done"] == 1
    assert status(e)[0] is RunStatus.COMPLETED and len(e.calls) == 1


def test_stale_completion_write_cannot_overwrite_the_new_owner(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    e.clock.advance(seconds=60)
    a_service, b_service = e.service, restart(e)
    resolver, b_resolver = (s._orch._executor.verification for s in (a_service, b_service))
    assert resolver is not None and b_resolver is not None
    key = ("crm.write_assessment", "2.0.0")
    read = resolver.adapters[key]
    # B's observation is distinguishable from A's, so the surviving result shows who won.
    b_resolver.adapters[key] = lambda *args: ReadbackResult(
        "disagreement", {"risk_level": "low"}, {"risk_level": "high"}, code="readback_mismatch"
    )
    seen: list[str] = []

    def slow_read(*args: Any) -> Any:
        observed = read(*args)
        if not seen:
            seen.append("a")
            e.clock.advance(seconds=31)  # runner A stalls past its lease mid-read
            only(runner(b_service).tick())  # runner B takes over and completes first
        return observed

    resolver.adapters[key] = slow_read
    beat = only(runner(a_service).tick())
    assert beat.contended == 1 and beat.outcomes.get("done", 0) == 0
    # B's completed result stands; A's older agreement was fenced by the job revision.
    assert e.job.result["outcome"] == "disagreement"
    assert status(e) == (RunStatus.FAILED, StepStatus.VERIFICATION_BLOCKED)
    assert e.job.evidence["observed"] == {"risk_level": "high"} and len(e.calls) == 1


def test_explicit_resume_and_startup_recovery_respect_the_runner_lease(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    e.clock.advance(seconds=60)
    resolver = e.orch._executor.verification
    assert resolver is not None
    key = ("crm.write_assessment", "2.0.0")
    read = resolver.adapters[key]
    reads: list[str] = []

    def counted(*args: Any) -> Any:
        reads.append("read")
        return read(*args)

    resolver.adapters[key] = counted
    sweep = runner(e.service)
    handler = sweep._handlers["write_readback"]
    original = handler.process

    def competing_drivers_first(job: MaintenanceJob) -> str:
        other = restart(e)
        other.recover_interrupted()  # startup recovery in another process
        other.resume_run(ALICE, e.run_id)  # explicit resume in another process
        assert reads == [] and e.job.result is None  # neither could read under our lease
        return original(job)

    handler.process = competing_drivers_first  # type: ignore[method-assign]
    assert only(sweep.tick()).outcomes["done"] == 1
    assert reads == ["read"] and status(e)[0] is RunStatus.COMPLETED and len(e.calls) == 1


# -- crash windows and restarts ----------------------------------------------------------


@pytest.mark.parametrize("boundary", ["result", "step", "audit", "continuation"])
def test_crash_after_evidence_commit_recovers_on_the_next_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    e = Env(tmp_path, downstream=True)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    e.clock.advance(seconds=60)
    resolver = e.orch._executor.verification
    assert resolver is not None
    store = e.stores.maintenance
    if boundary in {"result", "continuation"}:
        original_replace = store.replace

        def after_job(job: MaintenanceJob, **kwargs: Any) -> Any:
            value = original_replace(job, **kwargs)
            if job.kind == "write_readback" and (
                (boundary == "result" and job.result and job.evidence_seq is None)
                or (boundary == "continuation" and job.continued)
            ):
                raise Crash()
            return value

        monkeypatch.setattr(store, "replace", after_job)
    elif boundary == "step":
        original_transition = e.stores.steps.transition

        def after_step(*args: Any, **kwargs: Any) -> Any:
            value = original_transition(*args, **kwargs)
            if kwargs.get("to") is StepStatus.SUCCEEDED:
                raise Crash()
            return value

        monkeypatch.setattr(e.stores.steps, "transition", after_step)
    else:
        original_record = resolver.audit.record

        def after_audit(*args: Any, **kwargs: Any) -> Any:
            value = original_record(*args, **kwargs)
            if args[1] == "verification.readback":
                raise Crash()
            return value

        monkeypatch.setattr(resolver.audit, "record", after_audit)
    with pytest.raises(Crash):
        runner(e.service).tick()
    monkeypatch.undo()
    assert e.job.result is not None  # the evidence itself is durable
    e.clock.advance(seconds=31)
    only(runner(restart(e)).tick())
    assert status(e) == (RunStatus.COMPLETED, StepStatus.SUCCEEDED)
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.SUCCEEDED
    assert e.job.continued and e.job.evidence_seq is not None
    assert len(e.calls) == 1


def test_restart_with_due_work_after_dispatcher_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path)
    original = e.target.accept

    def after_accept(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(e.target, "accept", after_accept)
    with pytest.raises(Crash):
        e.advance()
    monkeypatch.undo()
    assert status(e) == (RunStatus.RUNNING, StepStatus.EXECUTING)
    e.clock.advance(seconds=31)  # the dispatcher's lease expired with the process
    beat = only(runner(restart(e)).tick())
    assert beat.outcomes["done"] == 1
    assert status(e) == (RunStatus.COMPLETED, StepStatus.SUCCEEDED) and len(e.calls) == 1


def test_no_redispatch_after_approval_expired_while_result_was_invisible(tmp_path: Path) -> None:
    e = Env(tmp_path, "mail", args=EXTERNAL)
    e.target.visible_at = e.clock.now + timedelta(seconds=240)
    e.service._approvals._default_ttl = timedelta(seconds=30)
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    e.approve()
    assert status(e)[0] is RunStatus.WAITING_VERIFICATION
    e.clock.advance(seconds=240)
    (approval,) = e.service._approval_store.for_step(e.run_id, "write")
    assert e.clock.now >= approval.expires_at
    beat = only(runner(e.service).tick())
    assert beat.outcomes["done"] == 1 and status(e)[0] is RunStatus.COMPLETED
    assert len(e.calls) == 1 and len(e.target.records()) == 1


def test_no_dispatch_when_intent_exists_but_write_never_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path, "mail", args=EXTERNAL)
    e.service._approvals._default_ttl = timedelta(seconds=30)
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    original = e.stores.executions.start

    def after_attempt(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise Crash()  # intent and attempt are durable; the send never happened

    monkeypatch.setattr(e.stores.executions, "start", after_attempt)
    with pytest.raises(Crash):
        e.approve()
    monkeypatch.undo()
    e.clock.advance(seconds=31)  # the approval has expired too
    sweep = runner(restart(e))
    assert only(sweep.tick()).outcomes["still_waiting"] == 1
    assert status(e) == (RunStatus.WAITING_VERIFICATION, StepStatus.AWAITING_READBACK)
    e.clock.now = e.job.deadline
    assert only(sweep.tick()).outcomes["newly_unknown"] == 1
    assert status(e)[0] is RunStatus.UNKNOWN
    assert e.calls == [] and e.target.records() == []  # unknown, never sent "to be sure"


def test_maintenance_handler_has_no_write_path(tmp_path: Path) -> None:
    e = Env(tmp_path)
    handler = runner(e.service)._handlers["write_readback"]
    assert isinstance(handler, ReadbackJobs)
    assert not any(hasattr(handler, name) for name in ("_executor", "_tools", "execute"))


def test_unplanned_runs_are_never_planned_by_the_runner(tmp_path: Path) -> None:
    e = Env(tmp_path)
    resolver = e.orch._executor.verification
    assert resolver is not None
    job = MaintenanceJob(
        job_id="f" * 64,
        kind="write_readback",
        tenant_id=ALICE.tenant_id,
        run_id=e.run_id,
        plan_version=1,
        step_id="write",
        action_hash="a" * 64,
        operation_key="b" * 64,
        created_at=e.clock.now,
        deadline=e.clock.now + timedelta(minutes=5),
        due_at=e.clock.now,
    )
    e.stores.maintenance.add(job)
    beat = only(runner(e.service).tick())
    assert beat.outcomes["skipped"] == 1
    assert e.stores.runs.get(e.run_id).status is RunStatus.CREATED and e.calls == []


def test_configured_deadline_is_bound_at_intent(tmp_path: Path) -> None:
    e = Env(tmp_path)
    plan = {
        "steps": [{"id": "write", "tool": "crm.write_assessment", "args": CRM, "depends_on": []}]
    }
    other = restart(
        e,
        FakeProvider([plan]),
        maintenance=MaintenanceSettings(interval_seconds=10, deadline_seconds=60),
    )
    e.target.visible_at = e.clock.now + timedelta(minutes=5)
    assert other._orch.advance(e.run_id).status is RunStatus.WAITING_VERIFICATION
    assert e.job.deadline - e.job.created_at == timedelta(seconds=60)
    e.clock.advance(seconds=60)
    assert only(runner(other).tick()).outcomes["newly_unknown"] == 1


# -- generic runner: fairness and kinds ----------------------------------------------------


class FakeKind:
    kind = "fake"

    def __init__(self, store: InMemoryMaintenanceStore, *, fail: set[str], outcome: str) -> None:
        self.store, self.fail, self.outcome = store, fail, outcome
        self.seen: list[str] = []

    def process(self, job: MaintenanceJob) -> str:
        self.seen.append(job.job_id)
        if job.job_id in self.fail:
            raise RuntimeError("cannot process")
        self.store.replace(
            job.model_copy(
                update={
                    "revision": job.revision + 1,
                    "result": {"outcome": self.outcome},
                    "continued": True,
                }
            ),
            expected_revision=job.revision,
        )
        return "newly_unknown" if self.outcome == "unknown" else "done"


def fake_job(n: int, *, kind: str = "fake", due: datetime = T0) -> MaintenanceJob:
    return MaintenanceJob(
        job_id=f"{n:064x}",
        kind=kind,
        tenant_id="t1",
        run_id="r1",
        plan_version=1,
        step_id=f"s{n}",
        action_hash="a" * 64,
        operation_key=f"op-{n}",
        created_at=T0,
        deadline=T0 + timedelta(minutes=5),
        due_at=due + timedelta(seconds=n),
    )


def test_failing_job_cannot_starve_later_jobs_in_one_tick() -> None:
    store, clock = InMemoryMaintenanceStore(), Clock()
    for n in range(10):
        store.add(fake_job(n))
    store.add(fake_job(99, kind="other"))
    kind = FakeKind(store, fail={fake_job(0).job_id}, outcome="agreement")
    r = MaintenanceRunner(store=store, handlers=[kind], clock=clock, batch_size=3, max_jobs=50)
    clock.advance(minutes=1)
    beat = only_fake(r.tick())
    assert beat.checked == 10 and beat.outcomes["done"] == 9 and beat.errors == 1
    failed = store.get("t1", fake_job(0).job_id)
    assert failed is not None and failed.lease_owner is None
    assert failed.due_at == clock.now + timedelta(seconds=30)  # moved to the back
    untouched = store.get("t1", fake_job(99, kind="other").job_id)
    assert untouched is not None and untouched.revision == 0  # unregistered kind never claimed


def test_failing_jobs_move_back_so_bounded_ticks_reach_later_work() -> None:
    store, clock = InMemoryMaintenanceStore(), Clock()
    for n in range(6):
        store.add(fake_job(n))
    failing = {fake_job(0).job_id, fake_job(1).job_id}
    kind = FakeKind(store, fail=failing, outcome="agreement")
    r = MaintenanceRunner(store=store, handlers=[kind], clock=clock, batch_size=2, max_jobs=3)
    clock.advance(minutes=1)
    assert only_fake(r.tick()).outcomes["done"] == 1
    assert only_fake(r.tick()).outcomes["done"] == 3
    assert kind.seen[3:] == [fake_job(n).job_id for n in (3, 4, 5)]


def test_unknown_handler_label_is_counted_as_an_error() -> None:
    store, clock = InMemoryMaintenanceStore(), Clock()
    store.add(fake_job(0))

    class Odd:
        kind = "fake"

        def process(self, job: MaintenanceJob) -> str:
            return "fine"

    beat = only_fake(MaintenanceRunner(store=store, handlers=[Odd()], clock=clock).tick())
    assert beat.errors == 1 and beat.outcomes == {}


def only_fake(beats: list[Heartbeat]) -> Heartbeat:
    (beat,) = beats
    assert beat.kind == "fake"
    return beat


# -- heartbeat signals and the alarm -----------------------------------------------------


def test_heartbeat_log_and_metrics_carry_counts_not_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    e = Env(tmp_path, "mail")
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    e.clock.advance(seconds=60)
    resolver = e.orch._executor.verification
    assert resolver is not None
    telemetry = InMemoryTelemetry()
    r = MaintenanceRunner(
        store=e.stores.maintenance,
        handlers=[ReadbackJobs(resolver, e.orch.advance)],
        telemetry=telemetry,
        clock=e.clock,
    )
    with caplog.at_level(logging.INFO, logger="gawe.maintenance"):
        r.tick()
    (record,) = [rec for rec in caplog.records if hasattr(rec, "heartbeat")]
    fields: dict[str, Any] = record.heartbeat  # type: ignore[attr-defined]
    assert fields["event"] == "gawe.maintenance.heartbeat" and fields["status"] == "ok"
    assert fields["kind"] == "write_readback" and fields["checked"] == 1
    assert fields["outcomes"]["done"] == 1 and fields["outcomes"]["blocked"] == 0
    assert fields["unknown_total"] == 0 and fields["unresolved_total"] == 0
    assert {"started_at", "finished_at", "duration_seconds"} <= set(fields)
    assert json.loads(record.getMessage().split(" ", 1)[1]) == fields
    text = caplog.text + json.dumps([telemetry.counts, telemetry.observations], default=str)
    for secret in (MAIL["subject"], MAIL["body"], MAIL["to"], ALICE.tenant_id, e.run_id):
        assert secret not in text
    assert telemetry.total("gawe.maintenance.heartbeat", kind="write_readback", status="ok") == 1
    assert telemetry.total("gawe.maintenance.jobs", outcome="done") == 1
    backlog = [
        (v, a["state"]) for n, v, a in telemetry.observations if n == "gawe.maintenance.backlog"
    ]
    assert sorted(backlog, key=lambda x: x[1]) == [(0, "unknown"), (0, "waiting")]


@pytest.mark.parametrize("broken", ["due", "counts"])
def test_failed_tick_emits_an_error_heartbeat_not_a_zero(
    broken: str, caplog: pytest.LogCaptureFixture
) -> None:
    store, clock = InMemoryMaintenanceStore(), Clock()

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("database unreachable")

    setattr(store, broken, unavailable)
    telemetry = InMemoryTelemetry()
    kind = FakeKind(store, fail=set(), outcome="agreement")
    r = MaintenanceRunner(store=store, handlers=[kind], telemetry=telemetry, clock=clock)
    with caplog.at_level(logging.INFO, logger="gawe.maintenance"):
        beat = only_fake(r.tick())
    assert beat.status == "error" and beat.error == "ConnectionError"
    assert beat.unknown_total is None and beat.unresolved_total is None
    assert telemetry.total("gawe.maintenance.heartbeat", status="error") == 1
    assert telemetry.total("gawe.maintenance.heartbeat", status="ok") == 0
    assert not telemetry.observed("gawe.maintenance.backlog")
    beats = [rec for rec in caplog.records if hasattr(rec, "heartbeat")]
    assert [rec.levelno for rec in beats] == [logging.ERROR]


def test_alarm_on_unknown_growth_and_separately_on_stale_or_missing_heartbeat() -> None:
    store, clock = InMemoryMaintenanceStore(), Clock()
    kind = FakeKind(store, fail=set(), outcome="unknown")
    r = MaintenanceRunner(store=store, handlers=[kind], clock=clock)
    stale_after = timedelta(seconds=90)
    assert r.health(clock.now, stale_after=stale_after)["fake"].heartbeat == "missing"

    r.tick()
    first = r.health(clock.now, stale_after=stale_after)["fake"]
    assert first.heartbeat == "ok" and not first.unknown_growing

    store.add(fake_job(1, due=clock.now))
    clock.advance(seconds=30)
    r.tick()  # one new unknown between two successful heartbeats: the alarm condition
    growing = r.health(clock.now, stale_after=stale_after)["fake"]
    assert growing.heartbeat == "ok" and growing.unknown_growing

    clock.advance(seconds=30)
    r.tick()  # retained, not growing: no alarm, and the count has not disappeared
    steady = r.health(clock.now, stale_after=stale_after)["fake"]
    assert not steady.unknown_growing and r.recent()[-1].unknown_total == 1

    clock.advance(seconds=91)  # the scheduler stopped: stale, distinct from growth
    stopped = r.health(clock.now, stale_after=stale_after)["fake"]
    assert stopped.heartbeat == "stale" and not stopped.unknown_growing

    store.add(fake_job(2, due=clock.now))
    real_counts = store.counts
    store.counts = lambda kind: (_ for _ in ()).throw(ConnectionError())  # type: ignore[method-assign]
    r.tick()
    failing = r.health(clock.now, stale_after=stale_after)["fake"]
    assert failing.heartbeat == "error"
    store.counts = real_counts  # type: ignore[method-assign]
    clock.advance(seconds=30)
    r.tick()  # the error tick neither hides nor resets growth between successful ticks
    recovered = r.health(clock.now, stale_after=stale_after)["fake"]
    assert recovered.heartbeat == "ok" and recovered.unknown_growing


def test_assess_orders_heartbeats_and_requires_two_successful_ones() -> None:
    def beat(seconds: int, status: str, unknown: int | None) -> Heartbeat:
        at = T0 + timedelta(seconds=seconds)
        return Heartbeat(
            kind="k",
            status=status,  # type: ignore[arg-type]
            started_at=at,
            finished_at=at,
            duration_seconds=0.0,
            unresolved_total=0 if unknown is not None else None,
            unknown_total=unknown,
        )

    now = T0 + timedelta(seconds=60)
    window = timedelta(seconds=90)
    assert not assess([beat(30, "ok", 4)], now=now, stale_after=window).unknown_growing
    rising = [beat(30, "ok", 5), beat(0, "ok", 4)]  # out of order on purpose
    assert assess(rising, now=now, stale_after=window).unknown_growing
    falling = [beat(0, "ok", 5), beat(30, "ok", 4)]
    assert not assess(falling, now=now, stale_after=window).unknown_growing
    assert assess([beat(0, "error", None)], now=now, stale_after=window).heartbeat == "error"


# -- settings, schedule and entry points ---------------------------------------------------


def test_settings_defaults_and_bounds() -> None:
    default = MaintenanceSettings.from_env({})
    assert default.interval == timedelta(seconds=30) and default.deadline == timedelta(minutes=5)
    custom = MaintenanceSettings.from_env(
        {"GAWE_MAINTENANCE_INTERVAL_SECONDS": "5", "GAWE_VERIFICATION_DEADLINE_SECONDS": "60"}
    )
    assert (custom.interval_seconds, custom.deadline_seconds) == (5, 60)
    for env in (
        {"GAWE_MAINTENANCE_INTERVAL_SECONDS": "0"},
        {"GAWE_MAINTENANCE_INTERVAL_SECONDS": "301"},
        {"GAWE_MAINTENANCE_INTERVAL_SECONDS": "ten"},
        {"GAWE_VERIFICATION_DEADLINE_SECONDS": "29"},
        {"GAWE_VERIFICATION_DEADLINE_SECONDS": "3601"},
        {"GAWE_MAINTENANCE_INTERVAL_SECONDS": "60", "GAWE_VERIFICATION_DEADLINE_SECONDS": "60"},
        {"GAWE_MAINTENANCE_BATCH_SIZE": "0"},
        {"GAWE_MAINTENANCE_MAX_JOBS": "10001"},
    ):
        with pytest.raises(SettingsError):
            MaintenanceSettings.from_env(env)


def test_api_lifespan_ticks_without_requests_and_stops_cleanly(tmp_path: Path) -> None:
    e = Env(tmp_path)
    sweep = runner(e.service)
    ticked = threading.Event()
    calls: list[int] = []
    real_tick = sweep.tick

    def tick() -> list[Heartbeat]:
        calls.append(1)
        if len(calls) >= 2:
            ticked.set()
        return real_tick()

    sweep.tick = tick  # type: ignore[method-assign]
    app = create_app(
        service=e.service,
        authenticator=TokenAuthenticator({}),
        maintenance_interval=0.01,
    )
    with TestClient(app):
        assert ticked.wait(timeout=5)  # no request was made
        ticker = app.state.maintenance_ticker
        assert isinstance(ticker, PeriodicTicker) and ticker.running
    assert not ticker.running
    stopped_at = len(calls)
    assert ticker._pool is not None and ticker._pool._shutdown  # type: ignore[attr-defined]
    assert len(calls) == stopped_at


def test_api_without_interval_starts_no_schedule(tmp_path: Path) -> None:
    app = create_app(service=Env(tmp_path).service, authenticator=TokenAuthenticator({}))
    with TestClient(app):
        assert app.state.maintenance_ticker is None


def test_ticker_survives_an_escaping_tick_failure() -> None:
    import asyncio

    done = threading.Event()
    calls: list[int] = []

    def tick() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("escaped")
        done.set()

    async def scenario() -> None:
        ticker = PeriodicTicker(tick, interval=0.01)
        ticker.start()
        await asyncio.get_running_loop().run_in_executor(None, done.wait, 5)
        await ticker.stop()
        assert not ticker.running

    asyncio.run(scenario())
    assert len(calls) >= 2


def test_cli_refuses_ephemeral_memory_and_requires_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GAWE_STORE", "memory")
    monkeypatch.setenv("GAWE_APP", "gawe.apps.contract_review:bundle")
    monkeypatch.setenv("GAWE_DEMO_DIR", str(tmp_path))
    monkeypatch.setenv("GAWE_POLICY_SIGNING_KEY", KEY.hex())
    assert cli.main(["sweep", "--once"]) == 2
    assert "ephemeral" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["sweep"])
    assert exit_info.value.code == 2


def test_cli_sweep_resolves_durable_work_without_a_model_or_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    e = Env(tmp_path, downstream=True)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    e.clock.advance(seconds=60)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a maintenance pass must not construct a model or plan")

    monkeypatch.setattr("gawe.llm.anthropic_provider.AnthropicProvider.__init__", forbidden)
    monkeypatch.setattr("gawe.llm.planner.Planner.__init__", forbidden)
    monkeypatch.setattr("gawe.llm.reader.Reader.__init__", forbidden)
    monkeypatch.setattr(cli, "_durable_stores", lambda: e.stores)
    monkeypatch.setattr(cli, "_load_bundle", lambda spec: e.bundle)
    monkeypatch.setenv("GAWE_APP", "tests:bundle")
    monkeypatch.setenv("GAWE_POLICY_SIGNING_KEY", KEY.hex())
    monkeypatch.delenv("GAWE_ANCHOR_SINKS", raising=False)
    assert cli.sweep_once(clock=e.clock) == 0
    beats = {b["kind"]: b for b in map(json.loads, capsys.readouterr().out.splitlines())}
    assert set(beats) == {"write_readback", "daily_review"}
    assert beats["write_readback"]["outcomes"]["done"] == 1
    assert status(e) == (RunStatus.COMPLETED, StepStatus.SUCCEEDED)
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.SUCCEEDED
    assert len(e.calls) == 1


def test_maintenance_only_orchestrator_refuses_to_plan(tmp_path: Path) -> None:
    from gawe.api.wiring import build_engine
    from gawe.engine.orchestrator import PlanningUnavailableError

    e = Env(tmp_path)
    engine = build_engine(e.bundle, stores=e.stores, signer=signer(), provider=None, clock=e.clock)
    with pytest.raises(PlanningUnavailableError):
        engine.orchestrator.advance(e.run_id)
    assert e.stores.runs.get(e.run_id).plan is None
