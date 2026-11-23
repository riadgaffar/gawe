"""M25 S3: random sequences of replays never pass the day's limit or apply part of a batch.

Each example plays a random sequence on one tenant's billing platform: replay batches of its
failed events (approved or rejected when they wait), while other actors correct an event's amount
or its original delivery lands late. After every step, plain invariants hold:

Waiting batches may be decided later, so policy's view of the day and of each event can be stale
by the time a batch is sent: the platform's own checks are what hold the invariants then.

* the day's replayed total never passes the platform's limit;
* that total is exactly what replays delivered, each event at most once;
* every replay applied its whole frozen batch (less what was already delivered) or nothing;
* no other tenant's event was ever delivered.
"""

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from hypothesis import example, given, seed, settings
from hypothesis import strategies as st

from tests.unit.test_apps_event_replay import FINANCE, T1, Desk, replay_ids

EVENTS = ["ev-1001", "ev-1002", "ev-1003", "ev-1004", "ev-1005"]  # the tenant's failed events
OTHER_TENANT = "ev-2001"

REPLAY = st.tuples(
    st.just("replay"),
    st.lists(st.sampled_from(EVENTS), min_size=1, max_size=5, unique=True),
    st.sampled_from(["approved", "rejected", "later"]),
)
CHANGE = st.tuples(st.just("change"), st.sampled_from(EVENTS), st.integers(0, 3_000))
LATE = st.tuples(st.just("late"), st.sampled_from(EVENTS), st.none())
# Decide every waiting batch now, after other actors and other replays may have moved on.
DECIDE = st.tuples(st.just("decide"), st.none(), st.sampled_from(["approved", "rejected"]))
STEPS = st.lists(st.one_of(REPLAY, REPLAY, CHANGE, LATE, DECIDE), min_size=1, max_size=10)


def deliveries(d: Desk) -> list[tuple[str, str, int, str]]:
    def read(conn: sqlite3.Connection) -> list[tuple[str, str, int, str]]:
        return list(conn.execute("SELECT event_id, tenant_id, amount, via FROM deliveries"))

    return d.billing.database.transaction(read)


def check(d: Desk, limit: int, replays: list[tuple[str, frozenset[str]]]) -> None:
    found = deliveries(d)
    replayed = [row for row in found if row[3] != "original"]
    total = d.billing.daily_total(T1)
    assert total <= limit
    assert total == sum(row[2] for row in replayed)
    assert len({row[0] for row in found}) == len(found)  # each event delivered at most once
    assert all(row[1] == T1 and row[0] != OTHER_TENANT for row in found)
    for key, frozen in replays:
        mine = {row[0] for row in replayed if row[3] == key}
        assert mine <= frozen
        if mine:  # applied: everything frozen is delivered, by this replay or before it
            assert {row[0] for row in found} >= frozen


def replays_so_far(d: Desk) -> list[tuple[str, frozenset[str]]]:
    """Each attempted replay's key and the events its batch froze."""
    found = []
    for run in d.stores.runs.list_for_tenant(T1):
        attempts = d.stores.executions.attempts(run.run_id, "replay")
        if attempts:
            batch = d.step(run.run_id).resolved_args["batch"]
            found.append((attempts[0].idempotency_key, frozenset(e["event_id"] for e in batch)))
    return found


@seed(15025)
@settings(max_examples=40, derandomize=True, database=None, deadline=None)
@example(
    limit=500,
    steps=[("replay", ["ev-1002"], "approved"), ("replay", ["ev-1001", "ev-1003"], "approved")],
)
@example(limit=3_000, steps=[("replay", ["ev-1004"], "approved"), ("replay", EVENTS, "approved")])
@example(
    limit=3_000,
    steps=[
        ("replay", ["ev-1004"], "later"),
        ("replay", ["ev-1002", "ev-1005"], "later"),
        ("decide", None, "approved"),
    ],
)
@example(
    limit=3_000,
    steps=[
        ("replay", ["ev-1001", "ev-1004"], "later"),
        ("change", "ev-1004", 10),
        ("decide", None, "approved"),
    ],
)
@given(limit=st.sampled_from([500, 1_000, 3_000]), steps=STEPS)
def test_replays_never_pass_the_limit_or_apply_part_of_a_batch(
    limit: int, steps: list[tuple[str, Any, Any]]
) -> None:
    with TemporaryDirectory(prefix="gawe-replays-") as tmp:
        d = Desk(Path(tmp), limit=limit)
        for kind, target, choice in steps:
            if kind == "replay":
                view = d.run(replay_ids(*target))
                if view.status == "paused_for_approval" and choice != "later":
                    (item,) = [i for i in d.svc.inbox(FINANCE) if i.run_id == view.run_id]
                    d.svc.decide(
                        FINANCE, item.approval_id, decision=choice, action_hash=item.action_hash
                    )
            elif kind == "decide":
                for item in d.svc.inbox(FINANCE):
                    d.svc.decide(
                        FINANCE, item.approval_id, decision=choice, action_hash=item.action_hash
                    )
            elif kind == "change":
                d.billing.change_event(target, {"amount": choice})
            elif d.billing.event(target)[0]["status"] == "failed":
                d.billing.deliver_late(target)
            check(d, limit, replays_so_far(d))
