"""M25 S1: the mock billing platform and its dead-letter queue.

The replay write is all or nothing, once per GAWE key, never bills an event twice under its own
delivery key, checks every event's revision inside its transaction, and reserves the batch's
total against the tenant's daily limit in that same transaction.
"""

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gawe.connectors.base import ConnectorError, Ledger, TransientConnectorError
from gawe.connectors.mock_billing import LISTED, MockBilling

T1 = "00000000-0000-0000-0000-00000000aa01"
T2 = "00000000-0000-0000-0000-00000000bb02"
NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def platform(tmp_path: Path, limit: int = 50_000, clock: Clock | None = None) -> MockBilling:
    return MockBilling.from_seed(
        Ledger(tmp_path / "ledger.jsonl"),
        daily_limits={T1: limit, T2: limit},
        clock=clock or Clock(),
    )


def item(b: MockBilling, event_id: str) -> dict[str, Any]:
    """What preparation would freeze for one event."""
    record, revision = b.event(event_id)
    return {
        "event_id": event_id,
        "revision": revision,
        "delivery_key": record["delivery_key"],
        "amount": record["amount"],
    }


def deliveries(b: MockBilling, *event_ids: str) -> list[str | None]:
    return [
        (d or {}).get("via") for d in (b.delivery(b.event(e)[0]["delivery_key"]) for e in event_ids)
    ]


# --- the queue ----------------------------------------------------------------------------------


def test_the_queue_lists_the_tenants_failed_events_oldest_first_without_payloads(
    tmp_path: Path,
) -> None:
    b = platform(tmp_path)
    listed = b.list_failed(T1)
    assert [e["event_id"] for e in listed] == [
        "ev-1001",
        "ev-1002",
        "ev-1003",
        "ev-1004",
        "ev-1005",
    ]
    assert all(set(e) == set(LISTED) for e in listed)  # no payload, no tenant, no key
    assert [e["event_id"] for e in b.list_failed(T2)] == ["ev-2001"]
    northwind = b.list_failed(T1, customer="cus-northwind", event_type="invoice.paid")
    assert [e["event_id"] for e in northwind] == ["ev-1001", "ev-1002", "ev-1003"]
    window = b.list_failed(T1, since="2026-11-20T09:05:00Z", until="2026-11-20T09:15:00Z")
    assert [e["event_id"] for e in window] == ["ev-1002", "ev-1003", "ev-1004"]


# --- the replay write ---------------------------------------------------------------------------


def test_a_batch_is_applied_whole_once_per_key(tmp_path: Path) -> None:
    b = platform(tmp_path)
    batch = [item(b, "ev-1001"), item(b, "ev-1002")]
    first = b.replay("k1", T1, batch, run_id="r1")
    assert first["applied"] == ["ev-1001", "ev-1002"] and first["total"] == 460
    again = b.replay("k1", T1, batch, run_id="r1")  # a retry after a lost reply
    assert again == first
    assert deliveries(b, "ev-1001", "ev-1002") == ["k1", "k1"]
    assert b.daily_total(T1) == 460
    assert b.event("ev-1001")[0]["status"] == "delivered"
    assert b.list_failed(T1, customer="cus-northwind")[0]["event_id"] == "ev-1003"


def test_the_readers_a_b_c_case(tmp_path: Path) -> None:
    """Preview A and B; B changes; C fails later. Nothing is applied; a new preview of A and B
    runs; C was never in a batch."""
    b = platform(tmp_path)
    previewed = [item(b, "ev-1001"), item(b, "ev-1002")]
    b.change_event("ev-1002", {"amount": 3400})  # someone corrects B
    with pytest.raises(ConnectorError) as refused:
        b.replay("k1", T1, previewed, run_id="r1")
    assert refused.value.code == "event_changed"
    assert deliveries(b, "ev-1001", "ev-1002") == [None, None] and b.daily_total(T1) == 0
    fresh = [item(b, "ev-1001"), item(b, "ev-1002")]
    assert b.replay("k2", T1, fresh, run_id="r2")["total"] == 3520
    assert deliveries(b, "ev-1003") == [None]  # C


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda i: {**i, "revision": "0" * 32}, "event_changed"),
        (lambda i: {**i, "amount": 1}, "event_changed"),
        (lambda i: {**i, "delivery_key": "dk-other"}, "event_changed"),
        (lambda i: {**i, "event_id": "ev-9999"}, "event_not_found"),
    ],
    ids=["revision", "amount", "delivery key", "unknown event"],
)
def test_one_bad_item_refuses_the_whole_batch(tmp_path: Path, change: Any, code: str) -> None:
    b = platform(tmp_path)
    batch = [item(b, "ev-1001"), change(item(b, "ev-1002"))]
    with pytest.raises(ConnectorError) as refused:
        b.replay("k1", T1, batch, run_id="r1")
    assert refused.value.code == code
    assert deliveries(b, "ev-1001") == [None] and b.daily_total(T1) == 0


def test_another_tenants_event_or_one_not_failed_refuses_the_batch(tmp_path: Path) -> None:
    b = platform(tmp_path)
    for other, code in [("ev-2001", "event_other_tenant"), ("ev-1006", "event_not_failed")]:
        with pytest.raises(ConnectorError) as refused:
            b.replay(f"k-{other}", T1, [item(b, "ev-1001"), item(b, other)], run_id="r1")
        assert refused.value.code == code
    assert deliveries(b, "ev-1001", "ev-2001") == [None, None]


@pytest.mark.parametrize("batch", [[], ["ev-1001", "ev-1001"]], ids=["empty", "named twice"])
def test_an_empty_batch_or_one_naming_an_event_twice_is_refused(
    tmp_path: Path, batch: list[str]
) -> None:
    b = platform(tmp_path)
    with pytest.raises(ConnectorError) as refused:
        b.replay("k1", T1, [item(b, e) for e in batch], run_id="r1")
    assert refused.value.code == "batch_invalid" and b.daily_total(T1) == 0


def test_a_late_original_delivery_is_skipped_never_billed_twice(tmp_path: Path) -> None:
    b = platform(tmp_path)
    batch = [item(b, "ev-1001"), item(b, "ev-1002")]
    b.deliver_late("ev-1001")  # the original callback lands between preview and write
    result = b.replay("k1", T1, batch, run_id="r1")
    assert result["applied"] == ["ev-1002"] and result["already_delivered"] == ["ev-1001"]
    assert result["total"] == 340 and b.daily_total(T1) == 340
    assert deliveries(b, "ev-1001", "ev-1002") == ["original", "k1"]


def test_a_lost_reply_leaves_one_operation_the_system_can_be_asked_about(tmp_path: Path) -> None:
    b = platform(tmp_path)
    batch = [item(b, "ev-1001")]
    b.operations.drop_reply = True
    with pytest.raises(TimeoutError):
        b.replay("k1", T1, batch, run_id="r1")
    record = b.operations.read("k1", NOW)
    assert record is not None and record["applied"] == ["ev-1001"]
    assert b.operations.read("never-sent", NOW) is None
    b.operations.drop_reply = False
    assert b.replay("k1", T1, batch, run_id="r1")["applied"] == ["ev-1001"]
    assert b.daily_total(T1) == 120


def test_a_fault_before_acceptance_applies_nothing(tmp_path: Path) -> None:
    b = platform(tmp_path)
    batch = [item(b, "ev-1001")]
    b.faults.fail_next()
    with pytest.raises(TransientConnectorError):  # safe to retry with the same key
        b.replay("k1", T1, batch, run_id="r1")
    assert deliveries(b, "ev-1001") == [None] and b.operations.read("k1", NOW) is None


# --- the daily limit ----------------------------------------------------------------------------


@pytest.mark.parametrize(("limit", "applied"), [(460, True), (459, False)])
def test_the_daily_limit_at_and_over(tmp_path: Path, limit: int, applied: bool) -> None:
    b = platform(tmp_path, limit=limit)
    batch = [item(b, "ev-1001"), item(b, "ev-1002")]
    if applied:
        b.replay("k1", T1, batch, run_id="r1")
        assert b.daily_total(T1) == limit and b.remaining_today(T1) == 0
    else:
        with pytest.raises(ConnectorError) as refused:
            b.replay("k1", T1, batch, run_id="r1")
        assert refused.value.code == "daily_limit_exceeded"
        assert b.daily_total(T1) == 0 and deliveries(b, "ev-1001") == [None]


def test_a_tenant_without_a_limit_replays_nothing(tmp_path: Path) -> None:
    b = MockBilling.from_seed(Ledger(tmp_path / "ledger.jsonl"), daily_limits={})
    with pytest.raises(ConnectorError) as refused:
        b.replay("k1", T1, [item(b, "ev-1001")], run_id="r1")
    assert refused.value.code == "no_daily_limit" and b.remaining_today(T1) is None


def test_the_limit_is_per_day(tmp_path: Path) -> None:
    clock = Clock()
    b = platform(tmp_path, limit=400, clock=clock)
    b.replay("k1", T1, [item(b, "ev-1002")], run_id="r1")  # 340
    with pytest.raises(ConnectorError):
        b.replay("k2", T1, [item(b, "ev-1001")], run_id="r2")  # 120 more
    clock.now += timedelta(days=1)
    b.replay("k3", T1, [item(b, "ev-1001")], run_id="r3")
    assert b.daily_total(T1) == 120


def test_two_batches_racing_for_the_last_of_the_limit_one_is_refused(tmp_path: Path) -> None:
    """Each fits alone; together they don't. The total is reserved inside the write's own
    transaction, so a check made outside it can't let both through."""
    b = platform(tmp_path, limit=400)
    first, second = [item(b, "ev-1002")], [item(b, "ev-1001"), item(b, "ev-1003")]  # 340, 215
    b.inside_write = lambda conn: time.sleep(0.1)
    outcomes: list[str] = []

    def go(key: str, batch: list[dict[str, Any]]) -> None:
        try:
            b.replay(key, T1, batch, run_id=key)
            outcomes.append("applied")
        except ConnectorError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=go, args=a) for a in [("k1", first), ("k2", second)]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["applied", "daily_limit_exceeded"]
    assert b.daily_total(T1) in {340, 215}


def test_a_delivery_changes_the_events_revision(tmp_path: Path) -> None:
    b = platform(tmp_path)
    _, before = b.event("ev-1001")
    b.replay("k1", T1, [item(b, "ev-1001")], run_id="r1")
    assert b.event("ev-1001")[1] != before
