"""A mock billing platform with its own dead-letter queue (M25).

Webhook deliveries that failed are kept as versioned event records; each carries the tenant, the
customer, the amount, the destination and its own delivery key, all set by the platform when the
event was raised. Replaying them is one billing write, in one SQLite transaction:

* an event already delivered under its own key (its original delivery landed late) is skipped,
  never billed twice;
* every other event must still have the revision the batch was prepared against, and still be
  failed, in the caller's tenant; otherwise nothing is applied;
* the batch's total is reserved against the tenant's daily limit, which the platform keeps; over
  it, nothing is applied;
* the batch is recorded once under GAWE's key, so a retry after a lost reply changes nothing;
* an empty batch, or one naming an event twice, is refused.

The queue and the billing ledger share one database on purpose: the revision check has to run
inside the write's own transaction. Were the queue a separate system, the check could only run
just before sending (a dispatch condition, M22), and the race in between would be named, not
closed.
"""

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from gawe.connectors.base import ConnectorError, FaultInjector, Ledger
from gawe.connectors.credentials import Credential, authorize
from gawe.connectors.mock_operations import MockDatabase, MockOperations, operations_table
from gawe.connectors.mock_records import VersionedRecords, records_table

SEED = Path(__file__).parent / "seeds" / "dead_letters.json"
LISTED = ("event_id", "type", "customer", "amount", "failed_at", "status")  # never the payload
_DELIVERIES = (
    "CREATE TABLE IF NOT EXISTS deliveries (delivery_key TEXT PRIMARY KEY, event_id TEXT NOT NULL, "
    "tenant_id TEXT NOT NULL, amount INTEGER NOT NULL, via TEXT NOT NULL, at TEXT NOT NULL)"
)
_DAILY = (
    "CREATE TABLE IF NOT EXISTS daily_totals (tenant_id TEXT NOT NULL, day TEXT NOT NULL, "
    "total INTEGER NOT NULL, PRIMARY KEY (tenant_id, day))"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MockBilling:
    """The platform: its dead-letter queue, its deliveries and its replay operations."""

    # What a tool's credential must hold for each operation (M23). The system checks it first.
    OPERATIONS: ClassVar[Mapping[str, str]] = {
        "list_failed": "event:read",
        "replay": "replay:write",
    }

    def authorize(self, credential: Credential, operation: str) -> None:
        authorize(self, credential, operation)

    GUARANTEES: Mapping[tuple[str, str], frozenset[str]] = {
        ("billing.replay_events", "1.0.0"): frozenset(
            {
                "idempotent_by_key",
                "authoritative_readback",
                "conditional_write",
                "atomic_reservation",
            }
        ),
    }

    GUARANTEES = {
        **GUARANTEES,
        ("billing.replay_events", "2.0.0"): GUARANTEES[("billing.replay_events", "1.0.0")],
    }

    def __init__(
        self,
        events: Sequence[Mapping[str, Any]],
        ledger: Ledger,
        *,
        daily_limits: Mapping[str, int],
        default_daily_limit: int | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        path = ledger.path.parent / "billing-system.sqlite3" if ledger.path else None
        self.database = MockDatabase(
            path,
            (records_table("events"), operations_table("replays"), _DELIVERIES, _DAILY),
        )
        self.events = VersionedRecords(
            self.database, "events", missing="event_not_found", normalize=str
        )
        self.events.seed((e["event_id"], e) for e in events)
        self.operations = MockOperations(ledger, "billing", database=self.database, table="replays")
        # The platform's own setting, per tenant; a tenant it doesn't list gets the default.
        self.daily_limits = dict(daily_limits)
        self.default_daily_limit = default_daily_limit
        self.faults = FaultInjector()
        self._ledger = ledger
        self._clock = clock
        # Test hook: runs inside the replay transaction, before any check.
        self.inside_write: Callable[[sqlite3.Connection], None] | None = None

    @classmethod
    def from_seed(
        cls,
        ledger: Ledger,
        *,
        daily_limits: Mapping[str, int],
        default_daily_limit: int | None = None,
        seed_file: Path = SEED,
        clock: Callable[[], datetime] = _utc_now,
    ) -> "MockBilling":
        data = json.loads(seed_file.read_text(encoding="utf-8"))
        return cls(
            data["events"],
            ledger,
            daily_limits=daily_limits,
            default_daily_limit=default_daily_limit,
            clock=clock,
        )

    def daily_limit(self, tenant_id: str) -> int | None:
        return self.daily_limits.get(tenant_id, self.default_daily_limit)

    # -- the dead-letter queue ------------------------------------------------------------------

    def list_failed(
        self,
        tenant_id: str,
        *,
        customer: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """The tenant's failed events matching the filters, oldest first, without payloads."""
        self.faults.check()
        found = [
            {k: e[k] for k in LISTED}
            for e in self.events.all().values()
            if e["tenant_id"] == tenant_id
            and e["status"] == "failed"
            and (customer is None or e["customer"] == customer)
            and (event_type is None or e["type"] == event_type)
            and (since is None or e["failed_at"] >= since)
            and (until is None or e["failed_at"] <= until)
        ]
        return sorted(found, key=lambda e: (e["failed_at"], e["event_id"]))

    def event(self, event_id: str) -> tuple[dict[str, Any], str]:
        """(record, revision) for trusted preparation: no faults, the payload included."""
        return self.events.get(event_id)

    def delivered(self, delivery_key: str) -> bool:
        """Whether a delivery under this key was ever applied, by any path."""
        return self.delivery(delivery_key) is not None

    def delivery(self, delivery_key: str) -> dict[str, Any] | None:
        def read(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT event_id, tenant_id, amount, via, at FROM deliveries WHERE delivery_key=?",
                (delivery_key,),
            ).fetchone()
            if row is None:
                return None
            return dict(zip(("event_id", "tenant_id", "amount", "via", "at"), row, strict=True))

        found: dict[str, Any] | None = self.database.transaction(read)
        return found

    def daily_total(self, tenant_id: str, day: date | None = None) -> int:
        day = day or self._clock().date()

        def read(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT total FROM daily_totals WHERE tenant_id=? AND day=?",
                (tenant_id, day.isoformat()),
            ).fetchone()
            return int(row[0]) if row else 0

        total: int = self.database.transaction(read)
        return total

    def remaining_today(self, tenant_id: str) -> int | None:
        """What is left of the tenant's daily limit; None when the platform sets no limit."""
        limit = self.daily_limit(tenant_id)
        return None if limit is None else max(limit - self.daily_total(tenant_id), 0)

    # -- test hooks: other actors ---------------------------------------------------------------

    def change_event(self, event_id: str, fields: Mapping[str, Any]) -> str:
        """Another actor edits an event (say, corrects its amount). Returns the new revision."""
        return self.events.replace(event_id, fields)

    def deliver_late(self, event_id: str) -> None:
        """The event's original delivery lands after all: applied under its own key, now."""

        def apply(conn: sqlite3.Connection) -> None:
            self._deliver(conn, event_id, via="original")

        self.events.transaction(apply)

    # -- the replay write -----------------------------------------------------------------------

    def replay(
        self, key: str, tenant_id: str, batch: Sequence[Mapping[str, Any]], *, run_id: str
    ) -> dict[str, Any]:
        """Replay a prepared batch: each item names an event, the revision it was prepared
        against, its delivery key and its amount. All or nothing; once per key."""
        self.faults.check()

        def write(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT record FROM replays WHERE operation_key=?", (key,)
            ).fetchone()
            if row:
                return dict(json.loads(row[0]))
            if self.inside_write is not None:
                self.inside_write(conn)
            applied: list[str] = []
            skipped: list[str] = []
            total = 0
            keys = [str(item["delivery_key"]) for item in batch]
            if not batch or len(set(keys)) != len(keys):
                raise ConnectorError("batch_invalid")  # empty, or an event named twice
            for item in batch:
                if self._delivered(conn, str(item["delivery_key"])):
                    skipped.append(str(item["event_id"]))
                    continue
                record = self._checked(conn, item, tenant_id)
                applied.append(record["event_id"])
                total += int(record["amount"])
            day = self._clock().date().isoformat()
            self._reserve(conn, tenant_id, day, total)
            for event_id in applied:
                self._deliver(conn, event_id, via=key)
            data = {
                "operation_key": key,
                "tenant_id": tenant_id,
                "run_id": run_id,
                "batch_id": f"rp-{key[:12]}",
                "applied": applied,
                "already_delivered": skipped,
                "total": total,
                "day": day,
            }
            conn.execute(
                "INSERT INTO replays VALUES (?, ?, ?)",
                (
                    key,
                    json.dumps(data, sort_keys=True),
                    (self.operations.visible_at or datetime.min.replace(tzinfo=UTC)).isoformat(),
                ),
            )
            return data

        data = self.events.transaction(write)
        for event_id in data["applied"]:
            entry: Callable[[], dict[str, Any]] = partial(
                dict, event_id=event_id, tenant_id=tenant_id
            )
            self._ledger.record_once("billing.delivery", f"{key}:{event_id}", entry)
        return self.operations.reply(
            {k: data[k] for k in ("batch_id", "applied", "already_delivered", "total")}
        )

    # -- inside the transaction -----------------------------------------------------------------

    @staticmethod
    def _delivered(conn: sqlite3.Connection, delivery_key: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM deliveries WHERE delivery_key=?", (delivery_key,)
        ).fetchone()
        return row is not None

    def _checked(
        self, conn: sqlite3.Connection, item: Mapping[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT record, revision FROM events WHERE record_id=?", (str(item["event_id"]),)
        ).fetchone()
        if row is None:
            raise ConnectorError("event_not_found")
        record, revision = dict(json.loads(row[0])), row[1]
        if record["tenant_id"] != tenant_id:
            raise ConnectorError("event_other_tenant")
        if revision != item["revision"]:
            try:
                observations = self.events.guard_observations(
                    str(item["event_id"]), record, revision
                )
            except Exception:
                observations = ()
            raise ConnectorError("event_changed", observations=observations)
        if record["status"] != "failed":
            raise ConnectorError("event_not_failed")
        if record["delivery_key"] != item["delivery_key"] or record["amount"] != item["amount"]:
            try:
                observations = self.events.guard_observations(
                    str(item["event_id"]), record, revision
                )
            except Exception:
                observations = ()
            raise ConnectorError("event_changed", observations=observations)
        return record

    def _reserve(self, conn: sqlite3.Connection, tenant_id: str, day: str, total: int) -> None:
        limit = self.daily_limit(tenant_id)
        if limit is None:
            raise ConnectorError("no_daily_limit")
        row = conn.execute(
            "SELECT total FROM daily_totals WHERE tenant_id=? AND day=?", (tenant_id, day)
        ).fetchone()
        used = int(row[0]) if row else 0
        if used + total > limit:
            raise ConnectorError("daily_limit_exceeded")
        conn.execute(
            "INSERT INTO daily_totals VALUES (?, ?, ?) "
            "ON CONFLICT (tenant_id, day) DO UPDATE SET total = total + excluded.total",
            (tenant_id, day, total),
        )

    def _deliver(self, conn: sqlite3.Connection, event_id: str, *, via: str) -> None:
        row = conn.execute("SELECT record FROM events WHERE record_id=?", (event_id,)).fetchone()
        if row is None:
            raise ConnectorError("event_not_found")
        record = dict(json.loads(row[0]))
        conn.execute(
            "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?, ?, ?, ?)",
            (
                record["delivery_key"],
                event_id,
                record["tenant_id"],
                int(record["amount"]),
                via,
                self._clock().isoformat(),
            ),
        )
        record["status"] = "delivered"
        conn.execute(
            "UPDATE events SET record=?, revision=lower(hex(randomblob(16))) WHERE record_id=?",
            (json.dumps(record, sort_keys=True), event_id),
        )
