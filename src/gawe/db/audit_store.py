"""Postgres implementation of the AuditStore protocol.

The store works on a caller-supplied Connection, so audit events commit or roll back together
with the rest of the caller's unit of work.

Concurrency: two writers extending the same run's chain race on (run_id, seq). The loser fails
in one of two ways, and both mean "someone else extended the chain; rebuild on the new head":

* it started while the winner's insert was uncommitted and blocks on the primary key, then gets
  SQLSTATE 23505 on `audit_events_pkey`;
* it started after the winner committed and the chain trigger rejects it with SQLSTATE 23514
  and a message beginning "audit chain:".

Anything else (malformed hash, unknown run, ...) is a real error and propagates unchanged. Each
append runs in a SAVEPOINT so a conflict does not poison the caller's transaction.
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from gawe.core.audit import ChainConflictError
from gawe.core.models import AuditEvent
from gawe.db.bind import Bind, connection_for

_COLUMNS = "seq, CAST(run_id AS text) AS run_id, type, actor, payload_hash, prev_hash, hash, ts"

_LAST = text(
    f"SELECT {_COLUMNS} FROM audit_events "
    "WHERE run_id = CAST(:run_id AS uuid) ORDER BY seq DESC LIMIT 1"
)
_EVENTS = text(
    f"SELECT {_COLUMNS} FROM audit_events WHERE run_id = CAST(:run_id AS uuid) ORDER BY seq ASC"
)
_INSERT = text(
    "INSERT INTO audit_events (run_id, seq, type, actor, payload_hash, prev_hash, hash, ts) "
    "VALUES (CAST(:run_id AS uuid), :seq, :type, :actor, :payload_hash, :prev_hash, :hash, :ts)"
)

_UNIQUE_VIOLATION = "23505"
_CHECK_VIOLATION = "23514"
_CHAIN_MESSAGE_PREFIX = "audit chain:"
_PRIMARY_KEY = "audit_events_pkey"


def is_chain_conflict(exc: DBAPIError) -> bool:
    """True if `exc` is the database rejecting an event that does not extend the chain."""
    original: Any = exc.orig
    sqlstate = getattr(original, "sqlstate", None)
    diag = getattr(original, "diag", None)
    if sqlstate == _UNIQUE_VIOLATION:
        return bool(getattr(diag, "constraint_name", None) == _PRIMARY_KEY)
    if sqlstate == _CHECK_VIOLATION:
        message = getattr(diag, "message_primary", None) or ""
        return str(message).startswith(_CHAIN_MESSAGE_PREFIX)
    return False


def _to_event(row: Any) -> AuditEvent:
    return AuditEvent(
        seq=row["seq"],
        run_id=row["run_id"],
        type=row["type"],
        actor=row["actor"],
        payload_hash=row["payload_hash"],
        prev_hash=row["prev_hash"],
        hash=row["hash"],
        ts=row["ts"],
    )


class PostgresAuditStore:
    """Pass a Connection to join the caller's transaction, or an Engine to commit per call."""

    def __init__(self, bind: Bind) -> None:
        self._bind = bind

    def last(self, run_id: str) -> AuditEvent | None:
        with connection_for(self._bind) as conn:
            row = conn.execute(_LAST, {"run_id": run_id}).mappings().first()
        return None if row is None else _to_event(row)

    def append(self, event: AuditEvent) -> None:
        params = {
            "run_id": event.run_id,
            "seq": event.seq,
            "type": event.type,
            "actor": event.actor,
            "payload_hash": event.payload_hash,
            "prev_hash": event.prev_hash,
            "hash": event.hash,
            "ts": event.ts,
        }
        try:
            with connection_for(self._bind) as conn, conn.begin_nested():
                conn.execute(_INSERT, params)
        except DBAPIError as exc:
            if is_chain_conflict(exc):
                raise ChainConflictError(
                    f"event seq {event.seq} does not extend the chain for run {event.run_id}"
                ) from exc
            raise

    def events(self, run_id: str) -> list[AuditEvent]:
        with connection_for(self._bind) as conn:
            rows = conn.execute(_EVENTS, {"run_id": run_id}).mappings().all()
        return [_to_event(row) for row in rows]
