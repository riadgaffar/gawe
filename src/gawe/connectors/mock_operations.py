"""A separate durable mock system of record, independent of the application ledger.

The accepted mutation and its idempotency record are one SQLite transaction. Records represent
the original operation, not a promise that another actor cannot subsequently change a resource.

A guarded write runs its precondition (for example "the contract's revision is still the one the
approval bound") inside that same `BEGIN IMMEDIATE` transaction, like HTTP `If-Match`: a write
from another process cannot land between the check and the acceptance.
"""

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from gawe.connectors.base import Ledger

T = TypeVar("T")
Guard = Callable[[sqlite3.Connection], None]
_TABLE = re.compile(r"[a-z][a-z0-9_]*")


class MockDatabase:
    """One SQLite database. Every action runs in its own `BEGIN IMMEDIATE` transaction."""

    def __init__(self, path: Path | None, schema: Sequence[str] = ()) -> None:
        self.path = path
        self._schema = tuple(schema)
        self._memory = sqlite3.connect(":memory:", check_same_thread=False) if not path else None
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return self.path is None or self.path.exists()

    def transaction(self, action: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            if self._memory is not None:
                conn = self._memory
            else:
                assert self.path is not None
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.path, timeout=10)
            try:
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                for statement in self._schema:
                    conn.execute(statement)
                result = action(conn)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
            finally:
                if conn is not self._memory:
                    conn.close()


def operations_table(table: str) -> str:
    if not _TABLE.fullmatch(table):
        raise ValueError(f"invalid table name {table!r}")
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (operation_key TEXT PRIMARY KEY, "
        "record TEXT NOT NULL, visible_at TEXT NOT NULL)"
    )


class MockOperations:
    def __init__(
        self,
        ledger: Ledger,
        system: str,
        *,
        database: MockDatabase | None = None,
        table: str = "operations",
    ) -> None:
        if database is None:
            path = ledger.path
            database = MockDatabase(
                path.parent / f"{system}-system.sqlite3" if path else None,
                (operations_table(table),),
            )
        operations_table(table)  # validates the name
        self.database = database
        self.path: Path | None = database.path
        self._table = table
        self.applied_override: dict[str, Any] = {}
        self.reply_override: dict[str, Any] = {}
        self.visible_at: datetime | None = None
        self.read_failures = 0
        self.drop_reply = False

    def accept(
        self, key: str, fields: Mapping[str, Any], *, guard: Guard | None = None
    ) -> dict[str, Any]:
        """Accept once per key. A guard runs only for a new operation, inside the transaction;
        an exception from it rolls everything back, so nothing is accepted."""

        def save(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                f"SELECT record FROM {self._table} WHERE operation_key=?", (key,)
            ).fetchone()
            if row:
                return dict(json.loads(row[0]))
            if guard is not None:
                guard(conn)
            data = {**fields, **self.applied_override, "operation_key": key}
            conn.execute(
                f"INSERT INTO {self._table} VALUES (?, ?, ?)",
                (
                    key,
                    json.dumps(data, sort_keys=True),
                    (self.visible_at or datetime.min.replace(tzinfo=UTC)).isoformat(),
                ),
            )
            return data

        return dict(self.database.transaction(save))

    def read(self, key: str, now: datetime) -> dict[str, Any] | None:
        if self.read_failures:
            self.read_failures -= 1
            raise OSError("mock system read unavailable")
        if not self.database.exists():
            return None

        def read(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                f"SELECT record, visible_at FROM {self._table} WHERE operation_key=?", (key,)
            ).fetchone()
            if not row or now < datetime.fromisoformat(row[1]):
                return None
            return dict(json.loads(row[0]))

        result: dict[str, Any] | None = self.database.transaction(read)
        return result

    def reply(self, output: Mapping[str, Any]) -> dict[str, Any]:
        if self.drop_reply:
            raise TimeoutError("mock response lost after acceptance")
        return {**output, **self.reply_override}

    def records(self) -> list[dict[str, Any]]:
        """Accepted effects, including operations not yet visible to verification reads."""
        if not self.database.exists():
            return []

        def read_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return [
                dict(json.loads(row[0]))
                for row in conn.execute(f"SELECT record FROM {self._table} ORDER BY rowid")
            ]

        result: list[dict[str, Any]] = self.database.transaction(read_all)
        return result
