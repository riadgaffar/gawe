"""Postgres history reader and subject locks (M24).

The reader selects a tenant's counted steps of one tool, any version, from every run's current
plan, and compares the subject in Python, so the comparison is exactly the one the in-memory
reader and the app's declaration use. The locks are session advisory locks, each on its own
connection, held while one step's history is read, its policy evaluated and its decision stored.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Engine

from gawe.core.history import HistoryEntry, HistorySubject, counts, entry
from gawe.db.step_store import _COLUMNS, _to_record

_NOT_COUNTED = ("pending", "ready", "denied", "rejected", "not_done_by_person")
_EARLIER = text(
    f"SELECT {_COLUMNS}, r.created_at AS started_at FROM steps s "
    "JOIN runs r ON r.id = s.run_id "
    "JOIN plans p ON p.id = s.plan_id AND p.version = r.current_plan_version "
    "WHERE r.tenant_id = CAST(:tenant AS uuid) AND s.tool_name = :tool "
    "AND s.action_hash IS NOT NULL AND s.status <> ALL(:not_counted) "
    "ORDER BY r.created_at, CAST(s.run_id AS text), s.step_key"
)
_LOCK = text("SELECT pg_advisory_lock(hashtextextended(:key, 0))")
_UNLOCK = text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))")


class PostgresHistoryReader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def earlier(
        self, tenant_id: str, tool: str, subject: HistorySubject, value: str
    ) -> list[HistoryEntry]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                _EARLIER,
                {"tenant": tenant_id, "tool": tool, "not_counted": list(_NOT_COUNTED)},
            ).mappings()
            found: list[HistoryEntry] = []
            for row in rows:
                step = _to_record(row)
                if counts(step) and subject.subject(step.resolved_args) == value:
                    found.append(entry(step, row["started_at"]))
        return found


class PostgresSubjectLocks:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def hold(self, key: str) -> Iterator[None]:
        with self._engine.connect() as conn:
            conn.execute(_LOCK, {"key": key})
            conn.commit()
            try:
                yield
            finally:
                conn.execute(_UNLOCK, {"key": key})
                conn.commit()
