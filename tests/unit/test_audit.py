import threading
from datetime import UTC, datetime, timedelta, timezone
from itertools import count
from typing import Any

import pytest

from gawe.core.audit import (
    GENESIS_HASH,
    AnchorPoint,
    AuditLog,
    ChainConflictError,
    InMemoryAuditStore,
    compute_event_hash,
    verify_chain,
)
from gawe.core.models import AuditEvent

T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def make_log(
    store: InMemoryAuditStore | None = None, **kw: Any
) -> tuple[AuditLog, InMemoryAuditStore]:
    store = store or InMemoryAuditStore()
    tick = count()
    log = AuditLog(store, clock=lambda: T0 + timedelta(seconds=next(tick)), **kw)
    return log, store


def build_chain(n: int = 4) -> list[AuditEvent]:
    log, store = make_log()
    for i in range(n):
        log.record("run1", f"type{i}", "alice", {"i": i})
    return store.events("run1")


# --------------------------------------------------------------------- chain


def test_chain_starts_at_genesis_and_links() -> None:
    events = build_chain(3)
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[0].prev_hash == GENESIS_HASH
    assert events[1].prev_hash == events[0].hash
    assert events[2].prev_hash == events[1].hash


def test_verify_ok_and_reports_head() -> None:
    events = build_chain(3)
    result = verify_chain(events)
    assert result.ok and result.checked == 3 and result.head == events[-1].hash


def test_empty_chain_verifies() -> None:
    result = verify_chain([])
    assert result.ok and result.checked == 0 and result.head is None


def test_hash_is_deterministic_and_covers_every_field() -> None:
    base: dict[str, Any] = {
        "seq": 1,
        "run_id": "r",
        "type": "t",
        "actor": "a",
        "payload_hash": "p" * 64,
        "prev_hash": GENESIS_HASH,
        "ts": T0,
    }
    h = compute_event_hash(**base)
    assert h == compute_event_hash(**base)
    changes = {
        "seq": 2,
        "run_id": "r2",
        "type": "t2",
        "actor": "a2",
        "payload_hash": "q" * 64,
        "prev_hash": "1" * 64,
        "ts": T0 + timedelta(microseconds=1),
    }
    for field, value in changes.items():
        assert compute_event_hash(**{**base, field: value}) != h, field


def test_hash_is_timezone_independent_but_rejects_naive() -> None:
    eastern = timezone(timedelta(hours=-4))
    kw: dict[str, Any] = {
        "seq": 1, "run_id": "r", "type": "t", "actor": "a",
        "payload_hash": "p" * 64, "prev_hash": GENESIS_HASH,
    }  # fmt: skip
    assert compute_event_hash(**kw, ts=T0) == compute_event_hash(**kw, ts=T0.astimezone(eastern))
    with pytest.raises(ValueError):
        compute_event_hash(**kw, ts=datetime(2026, 1, 1))


def test_payload_hash_ignores_key_order_and_none_means_empty() -> None:
    log, _ = make_log()
    a = log.record("r", "t", "a", {"x": 1, "y": 2})
    b = log.record("r", "t", "a", {"y": 2, "x": 1})
    c = log.record("r", "t", "a")
    d = log.record("r", "t", "a", {})
    assert a.payload_hash == b.payload_hash
    assert c.payload_hash == d.payload_hash != a.payload_hash


def test_payload_content_is_never_stored() -> None:
    log, store = make_log()
    log.record("r", "t", "a", {"clause": "SECRET TERMS"})
    assert "SECRET TERMS" not in repr(store.events("r"))


# --------------------------------------------------------------- tamper checks


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "other"),
        ("actor", "mallory"),
        ("payload_hash", "f" * 64),
        ("ts", T0 + timedelta(days=1)),
    ],
)
def test_editing_an_event_is_detected(field: str, value: Any) -> None:
    events = build_chain(4)
    events[1] = events[1].model_copy(update={field: value})
    result = verify_chain(events)
    assert not result.ok and result.failed_seq == 2


def test_rewriting_hash_to_match_is_still_detected_downstream() -> None:
    events = build_chain(4)
    edited = events[1].model_copy(update={"actor": "mallory"})
    events[1] = edited.model_copy(update={"hash": _recompute(edited)})
    result = verify_chain(events)
    assert not result.ok and result.failed_seq == 3  # next event's prev_hash no longer matches


def _recompute(e: AuditEvent) -> str:
    return compute_event_hash(
        seq=e.seq, run_id=e.run_id, type=e.type, actor=e.actor,
        payload_hash=e.payload_hash, prev_hash=e.prev_hash, ts=e.ts,
    )  # fmt: skip


def test_deleting_a_middle_event_is_detected() -> None:
    events = build_chain(4)
    del events[1]
    assert not verify_chain(events).ok


def test_deleting_the_first_event_is_detected() -> None:
    assert not verify_chain(build_chain(4)[1:]).ok


def test_reordering_is_detected() -> None:
    events = build_chain(4)
    events[1], events[2] = events[2], events[1]
    assert not verify_chain(events).ok


def test_duplicating_an_event_is_detected() -> None:
    events = build_chain(3)
    assert not verify_chain([*events, events[-1]]).ok


def test_event_from_another_run_is_detected() -> None:
    events = build_chain(3)
    other, _ = make_log()
    foreign = other.record("run2", "t", "a")
    assert not verify_chain([*events, foreign]).ok


def test_tail_truncation_needs_an_anchored_head() -> None:
    events = build_chain(4)
    head = events[-1].hash
    truncated = events[:2]
    assert verify_chain(truncated).ok  # known limitation: chain alone cannot see it
    result = verify_chain(truncated, expected_head=head)
    assert not result.ok and "anchored" in (result.reason or "")
    assert verify_chain(events, expected_head=head).ok


def test_anchored_head_on_empty_chain_fails() -> None:
    assert not verify_chain([], expected_head="a" * 64).ok


def _rewrite_from(events: list[AuditEvent], index: int, new_type: str) -> list[AuditEvent]:
    """What a database owner can do: change one event and recompute every later hash."""
    out = list(events[:index])
    prev = out[-1].hash if out else GENESIS_HASH
    for i, event in enumerate(events[index:]):
        changed = event.model_copy(
            update={"type": new_type if i == 0 else event.type, "prev_hash": prev}
        )
        fields = changed.model_dump(exclude={"hash"})
        rehashed = changed.model_copy(update={"hash": compute_event_hash(**fields)})
        out.append(rehashed)
        prev = rehashed.hash
    return out


def test_an_anchor_in_the_middle_of_the_chain_is_matched() -> None:
    events = build_chain(5)
    result = verify_chain(events, anchor=AnchorPoint(seq=3, head=events[2].hash))
    assert result.ok and result.anchored_seq == 3 and result.events_after_anchor == 2


def test_an_anchor_at_the_head_has_nothing_after_it() -> None:
    events = build_chain(3)
    result = verify_chain(events, anchor=AnchorPoint(seq=3, head=events[-1].hash))
    assert result.ok and result.events_after_anchor == 0


def test_truncation_below_the_anchor_is_detected() -> None:
    events = build_chain(4)
    anchor = AnchorPoint(seq=4, head=events[-1].hash)
    truncated = events[:3]
    assert verify_chain(truncated).ok  # the chain alone cannot see it
    result = verify_chain(truncated, anchor=anchor)
    assert not result.ok and result.reason == "chain is shorter than the anchored point"


def test_a_rewritten_history_with_recomputed_hashes_fails_against_the_anchor() -> None:
    events = build_chain(4)
    anchor = AnchorPoint(seq=4, head=events[-1].hash)
    forged = _rewrite_from(events, 1, "approval.granted")
    assert verify_chain(forged).ok  # internally consistent, so the chain alone is fooled
    result = verify_chain(forged, anchor=anchor)
    assert not result.ok
    assert result.reason == "event at the anchored point does not match the anchor"


def test_a_rewrite_after_the_anchor_does_not_touch_it() -> None:
    events = build_chain(4)
    anchor = AnchorPoint(seq=2, head=events[1].hash)
    forged = _rewrite_from(events, 3, "something.else")
    assert verify_chain(forged, anchor=anchor).ok  # only events up to the anchor are covered


def test_a_broken_chain_fails_even_when_the_anchor_matches() -> None:
    events = build_chain(4)
    anchor = AnchorPoint(seq=4, head=events[-1].hash)
    events[1] = events[1].model_copy(update={"actor": "mallory"})
    result = verify_chain(events, anchor=anchor)
    assert not result.ok and result.failed_seq == 2


@pytest.mark.parametrize("seq", [0, -1, 5])
def test_an_anchor_outside_the_chain_fails(seq: int) -> None:
    events = build_chain(4)
    assert not verify_chain(events, anchor=AnchorPoint(seq=seq, head=events[0].hash)).ok


def test_anchor_on_empty_chain_fails() -> None:
    assert not verify_chain([], anchor=AnchorPoint(seq=1, head="a" * 64)).ok


def test_audit_log_verify_and_last_pass_through() -> None:
    log, _ = make_log()
    assert log.last("run1") is None
    first = log.record("run1", "a", "alice")
    log.record("run1", "b", "alice")
    assert log.verify("run1", anchor=AnchorPoint(seq=1, head=first.hash)).events_after_anchor == 1
    assert log.last("run1") == log.events("run1")[-1]


# ------------------------------------------------------------------ the store


def test_store_rejects_events_that_do_not_extend_the_chain() -> None:
    events = build_chain(2)
    store = InMemoryAuditStore()
    with pytest.raises(ChainConflictError):  # first event must be seq 1 / genesis
        store.append(events[1])
    store.append(events[0])
    with pytest.raises(ChainConflictError):  # duplicate seq
        store.append(events[0])
    wrong_prev = events[1].model_copy(update={"prev_hash": "9" * 64})
    with pytest.raises(ChainConflictError):
        store.append(wrong_prev)
    store.append(events[1])


@pytest.mark.parametrize("field", ["run_id", "type", "actor"])
@pytest.mark.parametrize("value", ["", "x" * 129])
def test_record_validates_identifiers(field: str, value: str) -> None:
    log, _ = make_log()
    args = {"run_id": "r", "type": "t", "actor": "a", field: value}
    with pytest.raises(ValueError):
        log.record(**args)


def test_separate_runs_have_independent_chains() -> None:
    log, store = make_log()
    log.record("a", "t", "x")
    log.record("b", "t", "x")
    log.record("a", "t", "x")
    assert [e.seq for e in store.events("a")] == [1, 2]
    assert [e.seq for e in store.events("b")] == [1]
    assert log.verify("a").ok and log.verify("b").ok


# --------------------------------------------------------------- concurrency


class RacyStore(InMemoryAuditStore):
    """On the first append, a competing writer slips in an event, then this append conflicts."""

    def __init__(self) -> None:
        super().__init__()
        self.raced = False

    def append(self, event: AuditEvent) -> None:
        if not self.raced:
            self.raced = True
            AuditLog(self).record(event.run_id, "competitor", "other")
        super().append(event)


def test_conflict_is_retried_on_the_new_head() -> None:
    store = RacyStore()
    log = AuditLog(store)
    event = log.record("r", "mine", "me")
    chain = store.events("r")
    assert [e.type for e in chain] == ["competitor", "mine"]
    assert event.seq == 2 and verify_chain(chain).ok


class AlwaysConflicts(InMemoryAuditStore):
    def append(self, event: AuditEvent) -> None:
        raise ChainConflictError("nope")


def test_retries_are_bounded() -> None:
    with pytest.raises(ChainConflictError):
        AuditLog(AlwaysConflicts(), max_retries=2).record("r", "t", "a")


def test_concurrent_writers_produce_one_valid_chain() -> None:
    store = InMemoryAuditStore()
    log = AuditLog(store, max_retries=500)
    errors: list[BaseException] = []

    def work(worker: int) -> None:
        try:
            for i in range(10):
                log.record("r", "t", f"w{worker}", {"i": i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    chain = store.events("r")
    assert [e.seq for e in chain] == list(range(1, 81))
    assert verify_chain(chain).ok
