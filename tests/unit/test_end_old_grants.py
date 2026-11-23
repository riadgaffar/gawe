"""M21 S3: ending grants made before M16b with `end-old-grants`, then the sweep.

Each old grant here is a real one: the pre-M16b app sends `iam.grant_access@1.0.0` through the
engine, then the current app reads it back as succeeded. Nothing is planted in the stores except
where a test says so.
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_record_versions import _legacy_bundle, _service
from tests.unit.test_write_readback import BOB, Env

from gawe.core.expiry import KIND
from gawe.core.old_grants import job_id
from gawe.core.records import StepRecord
from gawe.core.runs import RunRecord
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.maintenance import __main__ as cli
from gawe.maintenance.old_grants import EVENT, OldGrantBackfill
from gawe.maintenance.runner import Heartbeat

OLD_V1 = ("iam.grant_access", "1.0.0")
LIFETIME = timedelta(days=7)
PRIVATE = ("dana", "wiki", '"read"', "acme.example")  # what a grant is about, never printed


class OldDesk:
    """An IT desk that granted access under 1.0.0, then upgraded."""

    def __init__(self, path: Path) -> None:
        self.e = Env(path, "iam")
        old = _service(self.e, _legacy_bundle(self.e, "iam"))
        old._orch.advance(self.e.run_id)
        if old.inbox(BOB):
            (item,) = old.inbox(BOB)
            old.decide(BOB, item.approval_id, decision="approved", action_hash=item.action_hash)
        self.service = _service(self.e, self.e.bundle)  # the current app
        self.service._orch.advance(self.e.run_id)
        step = self.step
        assert step.tool_version == "1.0.0" and step.status is StepStatus.SUCCEEDED
        (attempt,) = self.e.stores.executions.attempts(self.e.run_id, "write")
        self.grant_key = attempt.idempotency_key
        assert self.iam.grant_state(self.grant_key, self.e.clock.now) == "active"

    @property
    def iam(self) -> Any:
        return self.e.iam

    @property
    def step(self) -> StepRecord:
        return self.e.stores.steps.get(self.e.run_id, "write")

    def backfill(self) -> OldGrantBackfill:
        return OldGrantBackfill(
            versions=[OLD_V1],
            lifetime=LIFETIME,
            steps=self.e.stores.steps,
            executions=self.e.stores.executions,
            runs=self.e.stores.runs,
            jobs=self.e.stores.maintenance,
            audit=self.service._audit,
            anchorer=self.service._anchorer,
            clock=self.e.clock,
        )

    def jobs(self) -> list[Any]:
        return [j for j in self.e.stores.maintenance.for_run("t1", self.e.run_id) if j.kind == KIND]

    def events(self, kind: str = EVENT) -> list[Any]:
        return [e for e in self.e.stores.audit.events(self.e.run_id) if e.type == kind]

    def sweep(self) -> list[Heartbeat]:
        runner = self.service.maintenance_runner
        assert runner is not None
        return runner.tick()


def expiry_overdue(beats: list[Heartbeat]) -> int | None:
    (beat,) = [b for b in beats if b.kind == KIND]
    return beat.overdue_active


@pytest.fixture
def desk(tmp_path: Path) -> Iterator[OldDesk]:
    yield OldDesk(tmp_path)


# --- the dry run ---------------------------------------------------------------------------------


def test_the_dry_run_lists_the_grant_and_writes_nothing(desk: OldDesk) -> None:
    events_before = len(desk.e.stores.audit.events(desk.e.run_id))
    (item,) = desk.backfill().plan(7)
    assert (item.grant.run_id, item.grant.step_id, item.grant.grant_key) == (
        desk.e.run_id,
        "write",
        desk.grant_key,
    )
    # Made just now, with a 7-day lifetime and 7 days' notice: a tie, so its natural end.
    assert item.rule == "lifetime" and not item.job_exists
    assert desk.jobs() == []
    assert len(desk.e.stores.audit.events(desk.e.run_id)) == events_before
    printed = json.dumps(item.line())
    assert not any(word in printed for word in PRIVATE)


def test_an_older_grant_gets_the_notice_date(desk: OldDesk) -> None:
    desk.e.clock.advance(seconds=int(timedelta(days=30).total_seconds()))
    (item,) = desk.backfill().plan(10)
    assert item.rule == "notice" and item.end == desk.e.clock.now + timedelta(days=10)


# --- applying ------------------------------------------------------------------------------------


def test_apply_schedules_one_job_and_audits_once_however_often_it_runs(desk: OldDesk) -> None:
    desk.e.clock.advance(seconds=int(timedelta(days=30).total_seconds()))
    (first,) = desk.backfill().apply(7)
    (job,) = desk.jobs()
    assert job.due_at == first.end and job.payload["backfill"] == {
        "rule": "notice",
        "notice_days": 7,
    }
    assert job.payload["version"] == "1.0.0" and job.payload["grant_key"] == desk.grant_key
    assert len(desk.events()) == 1
    # Later, with another notice: the grant keeps the date it was given.
    desk.e.clock.advance(seconds=3600)
    (again,) = desk.backfill().apply(20)
    assert again.job_exists and again.end == first.end and again.rule == "notice"
    assert len(desk.jobs()) == 1 and len(desk.events()) == 1


def test_a_crash_between_the_job_and_its_event_is_repaired_by_the_next_run(
    desk: OldDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = desk.service._audit
    original = audit.record

    def crash(run_id: str, type: str, *args: Any, **kwargs: Any) -> Any:
        if type == EVENT:
            raise RuntimeError("crashed after the job")
        return original(run_id, type, *args, **kwargs)

    monkeypatch.setattr(audit, "record", crash)
    with pytest.raises(RuntimeError):
        desk.backfill().apply(7)
    assert len(desk.jobs()) == 1 and desk.events() == []
    monkeypatch.setattr(audit, "record", original)
    desk.backfill().apply(7)
    desk.backfill().apply(7)
    assert len(desk.jobs()) == 1 and len(desk.events()) == 1


def test_the_event_names_the_date_and_rule_never_the_grant(
    desk: OldDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = desk.service._audit
    original, seen = audit.record, []

    def spy(run_id: str, type: str, actor: str, payload: Any = None, **kwargs: Any) -> Any:
        if type == EVENT:
            seen.append(payload)
        return original(run_id, type, actor, payload, **kwargs)

    monkeypatch.setattr(audit, "record", spy)
    desk.backfill().apply(7)
    (payload,) = seen
    assert set(payload) == {"step_id", "grant_key", "end_date", "rule", "notice_days"}
    assert not any(word in json.dumps(payload) for word in PRIVATE)


def test_the_run_is_already_finished_so_its_anchor_moves_past_the_event(
    desk: OldDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert desk.e.stores.runs.get(desk.e.run_id).status is RunStatus.COMPLETED
    anchored: list[str] = []
    monkeypatch.setattr(
        desk.service._anchorer, "anchor_run", lambda run: anchored.append(run.run_id)
    )
    desk.backfill().apply(7)
    desk.backfill().apply(7)  # nothing new to anchor the second time
    assert anchored == [desk.e.run_id]


# --- the sweep -----------------------------------------------------------------------------------


def test_nothing_is_revoked_before_the_date_then_the_sweep_ends_it(desk: OldDesk) -> None:
    (item,) = desk.backfill().apply(7)
    desk.e.clock.now = item.end - timedelta(seconds=1)
    desk.sweep()
    assert desk.iam.grant_state(desk.grant_key, desk.e.clock.now) == "active"
    assert desk.events("grant.revoked") == []
    desk.e.clock.now = item.end
    beats = desk.sweep()
    assert desk.iam.grant_state(desk.grant_key, desk.e.clock.now) == "revoked"
    assert len(desk.events("grant.revoked")) == 1
    (job,) = desk.jobs()
    assert job.result == {"outcome": "revoked", "code": "grant_revoked"} and job.continued
    assert expiry_overdue(beats) == 0


def test_a_revoke_the_system_ignores_stays_waiting_and_raises_the_alarm(desk: OldDesk) -> None:
    (item,) = desk.backfill().apply(7)
    desk.iam.ignore_revokes = True
    desk.e.clock.now = item.end + timedelta(minutes=1)
    beats = desk.sweep()
    assert desk.iam.grant_state(desk.grant_key, desk.e.clock.now) == "active"
    assert expiry_overdue(beats) == 1  # the system doesn't know the date; GAWE's job does
    desk.iam.ignore_revokes = False
    desk.e.clock.advance(seconds=60)  # past the retry interval
    assert expiry_overdue(desk.sweep()) == 0
    assert desk.iam.grant_state(desk.grant_key, desk.e.clock.now) == "revoked"


def test_a_grant_already_removed_ends_as_revoked_without_a_second_revocation(
    desk: OldDesk,
) -> None:
    (item,) = desk.backfill().apply(7)
    desk.iam.revoke("by-hand", desk.grant_key, desk.e.clock.now)  # an operator got there first
    desk.e.clock.now = item.end
    desk.sweep()
    assert [r["revoke_key"] for r in desk.iam.revocations()] == ["by-hand"]
    (job,) = desk.jobs()
    assert job.result is not None and job.result["outcome"] == "revoked"


def test_an_unknown_outcome_that_never_landed_ends_as_not_granted(desk: OldDesk) -> None:
    # Planted: a 1.0.0 grant GAWE sent whose outcome nobody confirmed, and that never landed.
    stores, now = desk.e.stores, desk.e.clock.now
    stores.runs.add(
        RunRecord(
            run_id="run-lost",
            tenant_id="t1",
            acting_user_id="alice",
            objective="o",
            profile="it_access",
            created_at=now,
        )
    )
    stores.steps.add(
        StepRecord(
            run_id="run-lost",
            step_id="write",
            tool="iam.grant_access",
            tool_version="1.0.0",
            status=StepStatus.VERIFICATION_UNKNOWN,
            action_hash="c" * 64,
        )
    )
    stores.executions.start("run-lost", "write", attempt=1, idempotency_key="lost", now=now)
    planned = {i.grant.run_id: i for i in desk.backfill().apply(7)}
    assert set(planned) == {desk.e.run_id, "run-lost"}
    desk.e.clock.now = planned["run-lost"].end
    desk.sweep()
    lost = stores.maintenance.get("t1", job_id(planned["run-lost"].grant))
    assert lost is not None and lost.result is not None
    assert lost.result["outcome"] == "not_granted"
    assert desk.iam.grant_state("lost", desk.e.clock.now) == "absent"


# --- the command ---------------------------------------------------------------------------------


def test_the_command_refuses_memory_stores_and_a_bad_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GAWE_APP", "gawe.apps.it_access:bundle")
    monkeypatch.setenv("GAWE_DEMO_DIR", str(tmp_path))
    monkeypatch.setenv("GAWE_POLICY_SIGNING_KEY", "k" * 64)
    monkeypatch.setenv("GAWE_STORE", "memory")
    assert cli.end_old_grants(apply=False, out=StringIO()) == 2
    assert "memory" in capsys.readouterr().err
    assert cli.main(["end-old-grants", "--notice-days", "31"]) == 2
    assert "1 to 30 days" in capsys.readouterr().err


def test_the_command_prints_one_line_per_grant_and_a_total(
    desk: OldDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = desk.e.bundle
    monkeypatch.setattr(cli, "_engine", lambda clock: (bundle, desk.e.stores, _parts(desk)))
    out = StringIO()
    assert cli.end_old_grants(apply=False, clock=lambda: desk.e.clock.now, out=out) == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0]["run_id"] == desk.e.run_id and lines[0]["job"] == "new"
    assert lines[-1] == {
        "total": {
            "applied": False,
            "grants": 1,
            "new_jobs": 1,
            "by_rule": {"lifetime": 1},
            "notice_days": 7,
        }
    }
    assert not any(word in out.getvalue() for word in PRIVATE)
    assert desk.jobs() == []
    out = StringIO()
    assert cli.end_old_grants(apply=True, clock=lambda: desk.e.clock.now, out=out) == 0
    assert len(desk.jobs()) == 1


def _parts(desk: OldDesk) -> Any:
    class Parts:
        audit = desk.service._audit
        anchorer = desk.service._anchorer

    return Parts()
