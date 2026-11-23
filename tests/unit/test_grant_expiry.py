"""Grants expire: the system's own rule, a sweep that revokes and reads back, and an alarm."""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.flow import Clock
from tests.helpers import signer

from gawe.anchoring.file_sink import FileAnchorSink
from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.service import ApiService
from gawe.api.wiring import assemble, load_app_rules, memory_stores
from gawe.apps import it_access
from gawe.connectors.base import ConnectorError, Ledger
from gawe.connectors.mock_iam import MockIAM
from gawe.connectors.mock_tickets import MockTickets
from gawe.core.audit import verify_chain
from gawe.core.expiry import KIND, expiry_identity, revoke_identity
from gawe.core.maintenance import MaintenanceJob
from gawe.core.readback import operation_identity
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.llm.fake import FakeProvider
from gawe.maintenance.runner import Heartbeat, MaintenanceRunner, assess

ALICE = Identity("t1", "alice", frozenset({"it_agent"}))
BOB = Identity("t1", "bob", frozenset({"security_admin"}))
DANA = "dana.ortiz@acme.example"
WIKI = {"email": DANA, "system": "wiki", "level": "read"}
PROD = {"email": DANA, "system": "prod_db", "level": "read"}
LIFETIME = timedelta(hours=168)


def step(step_id: str, args: dict[str, Any], depends: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"id": step_id, "tool": "iam.grant_access", "args": args, "depends_on": list(depends)}


class Desk:
    """IT access over memory stores; `restart()` builds a new process over the same state."""

    def __init__(self, path: Path, *, steps: list[dict[str, Any]] | None = None) -> None:
        self.path = path
        self.clock = Clock()
        self.stores = memory_stores()
        self.iam = MockIAM.from_seed(
            it_access.HERE / "samples/iam_seed.json", Ledger(path / "ledger.jsonl")
        )
        self.sink = FileAnchorSink(path / "anchors.jsonl")
        self.tickets = MockTickets.in_state_dir(path)
        self.steps = steps or [step("write", WIKI)]
        self.service = self.restart([{"steps": self.steps}])
        self.run_id = "run-1"
        self.tickets.create(ALICE.tenant_id, "T-1", submitted_by=DANA, channel="portal", body="t")
        self.service._orch.start_run(
            tenant_id=ALICE.tenant_id,
            acting_user_id=ALICE.user_id,
            roles=tuple(ALICE.roles),
            objective="Grant access",
            profile=it_access.PROFILE_NAME,
            run_id=self.run_id,
            source_id="T-1",
        )

    def bundle(self) -> Any:
        b = it_access.bundle(self.path, iam=self.iam, tickets=self.tickets, clock=self.clock)
        return replace(
            b,
            variables=lambda run: {
                "tenant": {
                    "internal_domains": ["acme.example"],
                    "max_grant_lifetime_hours": 720,
                    "control_systems": ["crm"],
                },
                "request": {
                    "reported_requester": DANA,
                    "submitters": [DANA],
                    "portal_submitters": [DANA],
                },
            },
        )

    def restart(self, script: list[Any] | None = None) -> ApiService:
        return assemble(
            self.bundle(),
            stores=self.stores,
            signer=signer(),
            provider=FakeProvider(script or []),
            background=InlineBackground(),
            clock=self.clock,
            anchor_sinks=[self.sink],
        )

    @property
    def runner(self) -> MaintenanceRunner:
        assert self.service.maintenance_runner is not None
        return self.service.maintenance_runner

    def advance(self) -> RunStatus:
        return self.service._orch.advance(self.run_id).status

    def jobs(self, kind: str = KIND) -> list[MaintenanceJob]:
        return [
            j
            for j in self.stores.maintenance.for_run(ALICE.tenant_id, self.run_id)
            if j.kind == kind
        ]

    @property
    def job(self) -> MaintenanceJob:
        (job,) = self.jobs()
        return job

    def grant_key(self, step_id: str = "write") -> str:
        s = self.stores.steps.get(self.run_id, step_id)
        assert s.action_hash is not None
        return operation_identity(s.action_hash, self.stores.runs.get(self.run_id).plan_version)

    def events(self, kind: str = "grant.revoked") -> list[Any]:
        return [e for e in self.stores.audit.events(self.run_id) if e.type == kind]

    def to_expiry(self, extra: timedelta = timedelta(0)) -> None:
        self.clock.now = self.job.due_at + extra


def expiry_beat(beats: list[Heartbeat]) -> Heartbeat:
    (beat,) = [b for b in beats if b.kind == KIND]
    return beat


# --- the job exists before the write -------------------------------------------------------------


def test_expiry_job_is_created_before_the_grant_is_written(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    seen: list[list[MaintenanceJob]] = []
    original = d.iam.grant_access

    def watched(*args: Any, **kwargs: Any) -> Any:
        seen.append(d.jobs())  # what is durable at the moment of the write
        return original(*args, **kwargs)

    d.iam.grant_access = watched  # type: ignore[method-assign]
    assert d.advance() is RunStatus.COMPLETED
    (before,) = seen
    (job,) = before
    args = d.stores.steps.get(d.run_id, "write").resolved_args
    assert job.due_at == datetime.fromisoformat(args["expires_at"]) == d.clock.now + LIFETIME
    assert job.operation_key == revoke_identity(d.grant_key())
    assert job.job_id == expiry_identity(ALICE.tenant_id, d.run_id, "write", d.grant_key())
    assert job.payload["grant_key"] == d.grant_key() and job.result is None


def test_nothing_happens_before_the_end_date(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.advance()
    d.to_expiry(-timedelta(seconds=1))
    beat = expiry_beat(d.runner.tick())
    assert beat.checked == 0 and beat.overdue_active == 0
    assert d.iam.grant_state(d.grant_key(), d.clock.now) == "active"


# --- the three layers ----------------------------------------------------------------------------


def test_the_system_itself_treats_a_grant_as_inactive_at_its_end_date(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.advance()
    end = d.job.due_at
    assert d.iam.grant_state(d.grant_key(), end - timedelta(seconds=1)) == "active"
    assert d.iam.grant_state(d.grant_key(), end) == "expired"  # with no sweep at all
    d.iam.ignore_expiry = True  # the check this test guards
    assert d.iam.grant_state(d.grant_key(), end) == "active"


def test_the_sweep_revokes_at_the_end_date_and_reads_it_back(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    assert d.advance() is RunStatus.COMPLETED
    anchored = d.sink.get(d.run_id)
    assert anchored is not None
    d.to_expiry()
    beat = expiry_beat(d.runner.tick())
    assert beat.outcomes["revoked"] == 1 and beat.overdue_active == 0
    assert d.iam.grant_state(d.grant_key(), d.clock.now) == "revoked"
    assert d.job.result == {"outcome": "revoked", "code": "grant_revoked"} and d.job.continued
    (event,) = d.events()
    assert event.seq == d.job.evidence_seq
    events = d.stores.audit.events(d.run_id)
    assert verify_chain(events).ok and events[-1].type == "grant.revoked"
    refreshed = d.sink.get(d.run_id)
    assert refreshed is not None and refreshed.point.seq == event.seq > anchored.point.seq
    assert d.service.verify_audit(ALICE, d.run_id).ok  # anchored head still verifies
    assert expiry_beat(d.runner.tick()).checked == 0  # done work leaves the queue


def test_a_missing_job_leaves_the_grant_overdue_and_the_alarm_fires(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    end = d.clock.now - timedelta(hours=1)
    d.iam.operations.accept(  # a grant the engine never scheduled
        "orphan", {"email": DANA, "system": "wiki", "level": "read", "expires_at": end.isoformat()}
    )
    first = expiry_beat(d.runner.tick())
    assert first.overdue_active == 1 and first.checked == 0
    assert not d.runner.health(d.clock.now, stale_after=timedelta(minutes=5))[KIND].overdue_active
    d.clock.advance(seconds=30)
    expiry_beat(d.runner.tick())
    assert d.runner.health(d.clock.now, stale_after=timedelta(minutes=5))[KIND].overdue_active


def test_a_revoke_that_does_not_read_back_keeps_the_job_and_the_alarm(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.advance()
    d.iam.ignore_revokes = True
    d.to_expiry()
    beats = [expiry_beat(d.runner.tick())]
    d.clock.advance(seconds=30)
    beats.append(expiry_beat(d.runner.tick()))
    assert [b.outcomes["still_waiting"] for b in beats] == [1, 1]
    assert all(b.overdue_active == 1 for b in beats)
    assert d.job.result is None and d.events() == []
    assert d.runner.health(d.clock.now, stale_after=timedelta(minutes=5))[KIND].overdue_active
    d.iam.ignore_revokes = False  # the system recovers; the next tick finishes the job
    d.clock.advance(seconds=30)
    assert expiry_beat(d.runner.tick()).outcomes["revoked"] == 1
    d.clock.advance(seconds=30)
    expiry_beat(d.runner.tick())
    health = d.runner.health(d.clock.now, stale_after=timedelta(minutes=5))[KIND]
    assert not health.overdue_active


def test_alarm_needs_overdue_work_in_two_successful_heartbeats() -> None:
    t0 = datetime.fromisoformat("2026-11-15T00:00:00+00:00")

    def beat(minute: int, overdue: int | None, status: str = "ok") -> Heartbeat:
        at = t0 + timedelta(minutes=minute)
        return Heartbeat(
            kind=KIND,
            status=status,  # type: ignore[arg-type]
            started_at=at,
            finished_at=at,
            duration_seconds=0,
            unresolved_total=0,
            unknown_total=0,
            overdue_active=overdue,
        )

    now, window = t0 + timedelta(minutes=3), timedelta(minutes=5)
    assert not assess([beat(1, 1)], now=now, stale_after=window).overdue_active
    assert assess([beat(1, 1), beat(2, 2)], now=now, stale_after=window).overdue_active
    assert not assess([beat(1, 1), beat(2, 0)], now=now, stale_after=window).overdue_active
    assert not assess([beat(1, None), beat(2, None)], now=now, stale_after=window).overdue_active
    failed = [beat(1, 1), beat(2, None, "error"), beat(3, 1)]
    assert assess(failed, now=now, stale_after=window).overdue_active  # errors are skipped


def test_readback_heartbeats_carry_no_overdue_count(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    beats = {b.kind: b for b in d.runner.tick()}
    assert set(beats) == {"write_readback", KIND, "daily_review"}
    assert beats["write_readback"].overdue_active is None
    fields = beats[KIND].fields()
    assert fields["overdue_active"] == 0 and fields["outcomes"]["revoked"] == 0


# --- once, through restarts, races and crashes ----------------------------------------------------


def test_a_new_process_revokes_after_a_restart(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.advance()
    d.to_expiry()
    d.service = d.restart()
    assert expiry_beat(d.runner.tick()).outcomes["revoked"] == 1
    assert len(d.iam.revocations()) == 1 and len(d.events()) == 1


def test_two_sweepers_revoke_once(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.advance()
    d.to_expiry()
    first, second = d.runner, d.restart().maintenance_runner
    assert second is not None
    beats = [expiry_beat(first.tick()), expiry_beat(second.tick())]
    assert sum(b.outcomes.get("revoked", 0) for b in beats) == 1
    assert beats[1].checked == 0
    assert len(d.iam.revocations()) == 1 and len(d.events()) == 1


def test_a_crash_between_revoke_and_completion_revokes_once(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.advance()
    d.to_expiry()
    original = d.iam.revoke

    def revoke_then_fail(*args: Any) -> None:
        original(*args)
        raise ConnectionError("lost after the revoke landed")

    d.iam.revoke = revoke_then_fail  # type: ignore[method-assign]
    assert expiry_beat(d.runner.tick()).errors == 1
    assert len(d.iam.revocations()) == 1 and d.job.result is None
    d.iam.revoke = original  # type: ignore[method-assign]
    d.service = d.restart()
    d.clock.advance(seconds=30)
    assert expiry_beat(d.runner.tick()).outcomes["revoked"] == 1
    assert len(d.iam.revocations()) == 1 and len(d.events()) == 1


def test_a_crash_after_the_result_finishes_bookkeeping_without_a_second_event(
    tmp_path: Path,
) -> None:
    d = Desk(tmp_path)
    d.advance()
    d.to_expiry()
    original = d.stores.audit.append

    def fail_once(event: Any) -> None:
        if event.type == "grant.revoked":
            d.stores.audit.append = original  # type: ignore[method-assign]
            raise ConnectionError("audit store unavailable")
        original(event)

    d.stores.audit.append = fail_once  # type: ignore[method-assign]
    assert expiry_beat(d.runner.tick()).errors == 1
    assert d.job.result is not None and d.job.evidence_seq is None and not d.job.continued
    d.clock.advance(seconds=30)
    assert expiry_beat(d.runner.tick()).outcomes["continued"] == 1
    assert d.job.continued and len(d.events()) == 1 and len(d.iam.revocations()) == 1


def test_an_expired_lease_lets_another_sweeper_finish_and_the_first_cannot_redo_it(
    tmp_path: Path,
) -> None:
    d = Desk(tmp_path)
    d.advance()
    d.to_expiry()
    stalled = d.runner._claim(d.job, d.clock.now)  # a sweeper claimed it, then stalled
    assert stalled is not None
    assert expiry_beat(d.runner.tick()).checked == 0  # its lease is still held
    d.clock.advance(minutes=1)  # the lease ran out
    assert expiry_beat(d.restart().maintenance_runner.tick()).outcomes["revoked"] == 1  # type: ignore[union-attr]
    handler = next(h for h in d.runner._handlers.values() if h.kind == KIND)
    from gawe.core.maintenance import JobConflictError

    with pytest.raises(JobConflictError):
        handler.process(stalled)  # the stale worker wakes up; its write is refused
    assert len(d.iam.revocations()) == 1 and len(d.events()) == 1


# --- which grants --------------------------------------------------------------------------------


def test_a_cancelled_runs_grant_is_still_revoked(tmp_path: Path) -> None:
    d = Desk(tmp_path, steps=[step("write", WIKI), step("prod", PROD, ("write",))])
    assert d.advance() is RunStatus.PAUSED_FOR_APPROVAL
    d.service.cancel_run(ALICE, d.run_id)
    assert d.stores.runs.get(d.run_id).status is RunStatus.CANCELLED
    assert len(d.jobs()) == 1  # the undispatched grant never got a job
    d.to_expiry()
    assert expiry_beat(d.runner.tick()).outcomes["revoked"] == 1
    assert d.iam.grant_state(d.grant_key(), d.clock.now) == "revoked"


def test_a_grant_that_was_never_accepted_completes_as_not_granted(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    original = d.iam.grant_access

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise ConnectorError("record_changed")  # the system refused before accepting

    d.iam.grant_access = refused  # type: ignore[method-assign]
    assert d.advance() is RunStatus.FAILED
    d.iam.grant_access = original  # type: ignore[method-assign]
    assert d.stores.steps.get(d.run_id, "write").status is StepStatus.FAILED
    d.to_expiry()
    assert expiry_beat(d.runner.tick()).outcomes["not_granted"] == 1
    (event,) = d.events()
    assert d.job.result == {"outcome": "not_granted", "code": "grant_absent"}
    assert d.iam.grant_state(d.grant_key(), d.clock.now) == "absent"
    assert expiry_beat(d.runner.tick()).overdue_active == 0


def test_a_revocation_before_a_delayed_grant_still_applies(tmp_path: Path) -> None:
    iam = MockIAM.from_seed(it_access.HERE / "samples/iam_seed.json", Ledger(None))
    now = datetime.fromisoformat("2026-11-15T00:00:00+00:00")
    iam.revoke("rk", "late", now)
    iam.operations.accept("late", {"expires_at": (now + LIFETIME).isoformat()})
    assert iam.grant_state("late", now) == "revoked"


def test_grants_from_before_m16b_have_no_end_date(tmp_path: Path) -> None:
    iam = MockIAM.from_seed(it_access.HERE / "samples/iam_seed.json", Ledger(None))
    now = datetime.fromisoformat("2026-11-15T00:00:00+00:00")
    iam.operations.accept("old", {"email": DANA, "system": "wiki", "level": "read"})
    assert iam.grant_state("old", now + timedelta(days=3650)) == "active"
    assert iam.overdue(now + timedelta(days=3650)) == 0  # GAWE counts them from its jobs (M21)


# --- startup -------------------------------------------------------------------------------------


def test_a_write_with_an_end_date_needs_a_revoker(tmp_path: Path) -> None:
    b = it_access.bundle(tmp_path)
    with pytest.raises(ValueError, match="needs a revoker"):
        load_app_rules(replace(b, revokers={}))
    with pytest.raises(ValueError, match="unexpected"):
        load_app_rules(
            replace(
                b,
                revokers={
                    **b.revokers,
                    ("iam.lookup_user", "1.0.0"): b.revokers[("iam.grant_access", "2.0.0")],
                },
            )
        )
    assert load_app_rules(b)


def test_the_revocation_event_carries_no_argument_values(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.advance()
    d.to_expiry()
    args = d.stores.steps.get(d.run_id, "write").resolved_args
    payloads: list[dict[str, Any]] = []
    original = d.service._orch._audit.record

    def capture(run_id: str, type: str, actor: str, payload: Any = None, **kw: Any) -> Any:
        if type == "grant.revoked":
            payloads.append(dict(payload))
        return original(run_id, type, actor, payload, **kw)

    handler = next(h for h in d.runner._handlers.values() if h.kind == KIND)
    handler._audit.record = capture  # type: ignore[attr-defined]
    expiry_beat(d.runner.tick())
    (payload,) = payloads
    assert set(payload) == {"step_id", "grant_key", "revoke_key", "outcome"}
    text = str(payload)
    for value in (
        args["email"],
        args["system"],
        args["expires_at"],
        args["record"]["user_version"],
    ):
        assert str(value) not in text
