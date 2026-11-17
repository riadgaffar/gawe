"""A mock ticket system: who filed each ticket, through which channel, and what it says.

`submitted_by` and `channel` are the ticket system's own record, set when the ticket is created
and never changed afterwards; there is no method that changes them. The `portal` channel stands
for a signed-in employee filing the ticket, so its submitter is the system's word. `email` and
`public_form` tickets carry whatever the sender wrote. The body is attacker-writable text.

Tickets live in their own SQLite file in the mock state directory (in memory without one) and are
scoped to a tenant. Sample tickets are inserted the first time the system is read, so building a
connector writes nothing to disk.
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from gawe.connectors.mock_operations import MockDatabase
from gawe.core.sources import SourceError

Channel = Literal["portal", "email", "public_form"]
Status = Literal["open", "closed", "cancelled"]
CHANNELS: tuple[Channel, ...] = ("portal", "email", "public_form")
STATUSES: tuple[Status, ...] = ("open", "closed", "cancelled")
_TABLE = (
    "CREATE TABLE IF NOT EXISTS tickets (tenant_id TEXT NOT NULL, ticket_id TEXT NOT NULL, "
    "record TEXT NOT NULL, PRIMARY KEY (tenant_id, ticket_id))"
)


@dataclass(frozen=True)
class Ticket:
    tenant_id: str
    ticket_id: str
    submitted_by: str
    channel: Channel
    status: Status
    body: str


class MockTickets:
    def __init__(self, path: Path | None) -> None:
        self.database = MockDatabase(path, (_TABLE,))
        self._seed: list[Ticket] = []

    @classmethod
    def in_state_dir(cls, directory: Path | None) -> "MockTickets":
        return cls(directory / "tickets.sqlite3" if directory else None)

    def seed(self, tickets: Iterable[Ticket]) -> None:
        """Insert these on first use if absent; tickets already stored are kept as they are."""
        self._seed = list(tickets)

    def _insert_seed(self, conn: sqlite3.Connection) -> None:
        for ticket in self._seed:
            conn.execute(
                "INSERT OR IGNORE INTO tickets VALUES (?, ?, ?)",
                (ticket.tenant_id, ticket.ticket_id, json.dumps(asdict(ticket), sort_keys=True)),
            )

    def create(
        self,
        tenant_id: str,
        ticket_id: str,
        *,
        submitted_by: str,
        channel: Channel,
        body: str,
        status: Status = "open",
    ) -> Ticket:
        """File a new ticket. An existing ticket id is refused, never overwritten."""
        if channel not in CHANNELS or status not in STATUSES:
            raise ValueError("unknown ticket channel or status")
        ticket = Ticket(tenant_id, ticket_id, submitted_by, channel, status, body)

        def insert(conn: sqlite3.Connection) -> None:
            self._insert_seed(conn)
            try:
                conn.execute(
                    "INSERT INTO tickets VALUES (?, ?, ?)",
                    (tenant_id, ticket_id, json.dumps(asdict(ticket), sort_keys=True)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"ticket {ticket_id} already exists") from exc

        self.database.transaction(insert)
        return ticket

    def get(self, tenant_id: str, ticket_id: str) -> Ticket:
        def read(conn: sqlite3.Connection) -> str | None:
            self._insert_seed(conn)
            row = conn.execute(
                "SELECT record FROM tickets WHERE tenant_id=? AND ticket_id=?",
                (tenant_id, ticket_id),
            ).fetchone()
            return None if row is None else str(row[0])

        record = self.database.transaction(read)
        if record is None:
            raise SourceError("source_not_found")
        return Ticket(**json.loads(record))

    def set_status(self, tenant_id: str, ticket_id: str, status: Status) -> Ticket:
        """The one field that changes after filing."""
        if status not in STATUSES:
            raise ValueError("unknown ticket status")
        current = self.get(tenant_id, ticket_id)
        changed = Ticket(**{**asdict(current), "status": status})

        def update(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE tickets SET record=? WHERE tenant_id=? AND ticket_id=?",
                (json.dumps(asdict(changed), sort_keys=True), tenant_id, ticket_id),
            )

        self.database.transaction(update)
        return changed

    # -- DocumentSource ---------------------------------------------------------------------------

    def fetch(self, tenant_id: str, source_id: str) -> str:
        try:
            return self.get(tenant_id, source_id).body
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreadable ticket fails closed
            raise SourceError("source_unreadable") from exc
