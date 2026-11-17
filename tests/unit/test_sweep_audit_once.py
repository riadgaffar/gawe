"""A sweep's result is audited once, even when a worker that lost its lease records it again.

Each sweep saves its result, then audits it, then records the event's sequence. A worker that
stalls between the first two lets another finish the job; when it wakes it must not add a second
event. These tests stall the first worker exactly there.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_daily_review import ALWAYS, DAY, key
from tests.unit.test_daily_review import Desk as ReviewDesk
from tests.unit.test_grant_expiry import Desk as GrantDesk
from tests.unit.test_write_readback import Env

from gawe.core.audit import AuditLog, InMemoryAuditStore, verify_chain
from gawe.core.expiry import KIND as EXPIRY
from gawe.core.maintenance import JobConflictError
from gawe.core.models import AuditEvent
from gawe.core.review import KIND as REVIEW
from gawe.core.statemachine import RunStatus

RUN = "run-1"


# --- the write-once record --------------------------------------------------------------------


def test_once_returns_the_event_already_in_the_chain() -> None:
    audit = AuditLog(InMemoryAuditStore())
    first = audit.record(RUN, "sweep.done", "sweeper", {"job": 1}, once=True)
    again = audit.record(RUN, "sweep.done", "sweeper", {"job": 1}, once=True)
    assert again == first and len(audit.events(RUN)) == 1


@pytest.mark.parametrize(
    "type,actor,payload",
    [
        ("sweep.other", "sweeper", {"job": 1}),
        ("sweep.done", "someone", {"job": 1}),
        ("sweep.done", "sweeper", {"job": 2}),
    ],
)
def test_once_still_appends_a_different_event(type: str, actor: str, payload: Any) -> None:
    audit = AuditLog(InMemoryAuditStore())
    audit.record(RUN, "sweep.done", "sweeper", {"job": 1}, once=True)
    audit.record(RUN, type, actor, payload, once=True)
    assert len(audit.events(RUN)) == 2


def test_without_once_repeats_are_appended() -> None:
    audit = AuditLog(InMemoryAuditStore())
    audit.record(RUN, "note", "x", {"a": 1})
    audit.record(RUN, "note", "x", {"a": 1})
    assert len(audit.events(RUN)) == 2


class LateWriter(InMemoryAuditStore):
    """Another worker appends the same event right after this one has read the chain."""

    def __init__(self) -> None:
        super().__init__()
        self.other: AuditLog | None = None

    def events(self, run_id: str) -> list[AuditEvent]:
        chain = super().events(run_id)
        if self.other is not None:
            other, self.other = self.other, None
            other.record(run_id, "sweep.done", "sweeper", {"job": 1}, once=True)
        return chain


def test_a_writer_that_appends_between_the_check_and_the_append_wins_once() -> None:
    store = LateWriter()
    store.other = AuditLog(store)
    audit = AuditLog(store)
    event = audit.record(RUN, "sweep.done", "sweeper", {"job": 1}, once=True)
    (only,) = store.events(RUN)
    assert event == only and verify_chain([only]).ok


# --- each sweep, stalled between its result and its event -------------------------------------


def stall_first_record(log: AuditLog, kind: str, meanwhile: Any) -> None:
    """The first `kind` event this log writes waits while `meanwhile()` runs another worker."""
    original = log.record
    state = {"stalled": False}

    def record(run_id: str, type: str, actor: str, payload: Any = None, **kw: Any) -> Any:
        if type == kind and not state["stalled"]:
            state["stalled"] = True
            meanwhile()
        return original(run_id, type, actor, payload, **kw)

    log.record = record  # type: ignore[method-assign]


def test_read_back_records_its_evidence_once(tmp_path: Path) -> None:
    e = Env(tmp_path, "crm")
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    e.clock.advance(seconds=60)
    resolver = e.orch._executor.verification
    assert resolver is not None
    step = e.stores.steps.get(e.run_id, "write")
    before = [ev.type for ev in e.stores.audit.events(e.run_id)].count("verification.readback")
    assert before == 1  # the earlier "awaiting read-back" pass has its own event
    stall_first_record(resolver.audit, "verification.readback", lambda: resolver.resolve(step))
    with pytest.raises(JobConflictError):  # the stalled worker's bookkeeping is refused
        resolver.resolve(step)
    events = e.stores.audit.events(e.run_id)
    assert [ev.type for ev in events].count("verification.readback") == before + 1
    assert verify_chain(events).ok and e.job.evidence_seq is not None


def test_grant_expiry_records_its_revocation_once(tmp_path: Path) -> None:
    d = GrantDesk(tmp_path)
    d.advance()
    d.to_expiry()
    stalled = d.runner._claim(d.job, d.clock.now)
    assert stalled is not None
    handler = next(h for h in d.runner._handlers.values() if h.kind == EXPIRY)

    def another_sweeper() -> None:
        d.clock.advance(minutes=1)  # the stalled worker's lease runs out
        other = d.restart().maintenance_runner
        assert other is not None
        assert next(b for b in other.tick() if b.kind == EXPIRY).outcomes["continued"] == 1

    stall_first_record(handler._audit, "grant.revoked", another_sweeper)
    with pytest.raises(JobConflictError):
        handler.process(stalled)
    assert len(d.events()) == 1 and len(d.iam.revocations()) == 1
    assert d.job.continued and d.job.evidence_seq == d.events()[0].seq


def test_the_review_records_each_item_once(tmp_path: Path) -> None:
    d = ReviewDesk(tmp_path, key=key(), rate=ALWAYS)
    run_id = d.run()
    first = next(h for h in d.runner._handlers.values() if h.kind == REVIEW)
    other = d.runner_of(d.restart())  # a second process over the same stores
    second = next(h for h in other._handlers.values() if h.kind == REVIEW)
    stall_first_record(first._audit, "review.sampled", lambda: second.review("t1", DAY))
    first.review("t1", DAY)
    assert len(d.events(run_id, "review.sampled")) == 1
    (item,) = d.items()
    assert item.event_seq == d.events(run_id, "review.sampled")[0].seq
