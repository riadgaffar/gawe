"""Versioned records in a mock system of record.

Each record carries an opaque revision that changes on every mutation, even one that restores
earlier content, so an approval bound to a revision can never be satisfied by a record that was
edited in between. Seed data is inserted once; later starts keep what is stored.
"""

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import uuid4

from gawe.connectors.base import ConnectorError
from gawe.connectors.mock_operations import Guard, MockDatabase
from gawe.core.record_changes import RecordObservation, SnapshotSpec
from gawe.core.refusals import observe_record

T = TypeVar("T")
_TABLE = re.compile(r"[a-z][a-z0-9_]*")


def records_table(table: str) -> str:
    if not _TABLE.fullmatch(table):
        raise ValueError(f"invalid table name {table!r}")
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (record_id TEXT PRIMARY KEY, "
        "record TEXT NOT NULL, revision TEXT NOT NULL)"
    )


def _revision() -> str:
    return uuid4().hex


class VersionedRecords:
    def __init__(
        self, database: MockDatabase, table: str, *, missing: str, normalize: Callable[[str], str]
    ) -> None:
        records_table(table)  # validates the name
        self.database = database
        self._table = table
        self._missing = missing
        self._normalize = normalize
        self._seed_rows: list[tuple[str, str, str]] = []
        self.observation_spec: SnapshotSpec | None = None

    def seed(self, records: Iterable[tuple[str, Mapping[str, Any]]]) -> None:
        """Insert missing seed records lazily, in the first transaction that reads them, so
        building a connector writes nothing to disk."""
        self._seed_rows = [
            (self._normalize(rid), json.dumps(dict(r), sort_keys=True), _revision())
            for rid, r in records
        ]

    def _ensure_seeded(self, conn: sqlite3.Connection) -> None:
        if self._seed_rows:
            conn.executemany(
                f"INSERT OR IGNORE INTO {self._table} VALUES (?, ?, ?)", self._seed_rows
            )

    def _run(self, action: Callable[[sqlite3.Connection], T]) -> T:
        def seeded(conn: sqlite3.Connection) -> T:
            self._ensure_seeded(conn)
            return action(conn)

        return self.database.transaction(seeded)

    def transaction(self, action: Callable[[sqlite3.Connection], T]) -> T:
        """Run `action` in one transaction of the system's database, with the seed in place, so a
        write elsewhere in the system can check these records in its own transaction."""
        return self._run(action)

    def get(self, record_id: str) -> tuple[dict[str, Any], str]:
        """(record, revision); ConnectorError(missing) when absent."""

        def read(conn: sqlite3.Connection) -> tuple[str, str] | None:
            row = conn.execute(
                f"SELECT record, revision FROM {self._table} WHERE record_id=?",
                (self._normalize(record_id),),
            ).fetchone()
            return None if row is None else (row[0], row[1])

        row = self._run(read)
        if row is None:
            raise ConnectorError(self._missing)
        return dict(json.loads(row[0])), row[1]

    def all(self) -> dict[str, dict[str, Any]]:
        def read(conn: sqlite3.Connection) -> list[tuple[str, str]]:
            return list(conn.execute(f"SELECT record_id, record FROM {self._table}"))

        return {rid: dict(json.loads(r)) for rid, r in self._run(read)}

    def replace(
        self, record_id: str, fields: Mapping[str, Any], *, remove: Iterable[str] = ()
    ) -> str:
        """Test hook for another actor's change: merge fields, drop `remove`, issue a new
        revision."""
        return self._run(
            lambda conn: self.update_in(conn, record_id, set_fields=fields, remove_fields=remove)
        )

    def update_in(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        *,
        set_fields: Mapping[str, Any],
        remove_fields: Iterable[str] = (),
    ) -> str:
        """Change a record inside a caller's transaction (after its guard) and issue a new
        revision, so the system's own write moves the revision like anyone else's (M26)."""
        key = self._normalize(record_id)
        row = conn.execute(f"SELECT record FROM {self._table} WHERE record_id=?", (key,)).fetchone()
        if row is None:
            raise ConnectorError(self._missing)
        record = {**json.loads(row[0]), **set_fields}
        for name in remove_fields:
            record.pop(name, None)
        revision = _revision()
        conn.execute(
            f"UPDATE {self._table} SET record=?, revision=? WHERE record_id=?",
            (json.dumps(record, sort_keys=True), revision, key),
        )
        return revision

    def guard_observations(
        self, subject: str, record: Mapping[str, Any], revision: str
    ) -> tuple[RecordObservation, ...]:
        if self.observation_spec is None:
            return ()
        return (
            observe_record(
                self.observation_spec,
                self._normalize(subject),
                (record, revision),
                timing="at_guard",
                now=datetime.now(UTC),
            ),
        )

    def guard(
        self, record_id: str, revision: str, check: Callable[[dict[str, Any]], None] | None = None
    ) -> Guard:
        """A precondition for a guarded write: the record still has the bound revision."""

        def run(conn: sqlite3.Connection) -> None:
            self._ensure_seeded(conn)
            row = conn.execute(
                f"SELECT record, revision FROM {self._table} WHERE record_id=?",
                (self._normalize(record_id),),
            ).fetchone()
            if row is None:
                raise ConnectorError(self._missing)
            if row[1] != revision:
                try:
                    observations = self.guard_observations(
                        record_id, dict(json.loads(row[0])), row[1]
                    )
                except Exception:
                    observations = ()  # Diagnostic failure cannot erase the proven refusal.
                raise ConnectorError("record_changed", observations=observations)
            if check is not None:
                check(dict(json.loads(row[0])))

        return run
