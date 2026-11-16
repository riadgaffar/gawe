"""Postgres implementation of ReviewStore. Items are insert-once; a verdict is set once."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from gawe.core.review import (
    ReviewAlreadyDecidedError,
    ReviewItem,
    ReviewItemNotFoundError,
    Status,
    Verdict,
)
from gawe.db.bind import Bind, connection_for

_COLUMNS = (
    "item_id, CAST(tenant_id AS text) AS tenant_id, day, CAST(run_id AS text) AS run_id, "
    "step_key AS step_id, action_hash, reason, key_id, status, created_at, event_seq, "
    "CAST(decided_by AS text) AS decided_by, decided_at"
)
_INSERT = text(
    "INSERT INTO review_items (item_id, tenant_id, day, run_id, step_key, action_hash, reason, "
    "key_id, created_at) VALUES (:item_id, CAST(:tenant_id AS uuid), :day, "
    "CAST(:run_id AS uuid), :step_id, :action_hash, :reason, :key_id, :created_at) "
    "ON CONFLICT (item_id) DO NOTHING"
)
_GET = text(
    f"SELECT {_COLUMNS} FROM review_items "
    "WHERE item_id = :item_id AND tenant_id = CAST(:tenant_id AS uuid)"
)
_LIST = text(
    f"SELECT {_COLUMNS} FROM review_items WHERE tenant_id = CAST(:tenant_id AS uuid) "
    "AND (CAST(:status AS text) IS NULL OR status = :status) "
    "ORDER BY day, created_at, item_id LIMIT :limit"
)
_SET_EVENT = text(
    "UPDATE review_items SET event_seq = :seq "
    "WHERE item_id = :item_id AND tenant_id = CAST(:tenant_id AS uuid) AND event_seq IS NULL"
)
_DECIDE = text(
    "UPDATE review_items SET status = :verdict, decided_by = CAST(:decided_by AS uuid), "
    "decided_at = :decided_at "
    "WHERE item_id = :item_id AND tenant_id = CAST(:tenant_id AS uuid) AND status = 'open'"
)


def _uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _item(row: Any) -> ReviewItem:
    return ReviewItem.model_validate(dict(row))


class PostgresReviewStore:
    def __init__(self, bind: Bind) -> None:
        self._bind = bind

    def add(self, item: ReviewItem) -> ReviewItem:
        params = item.model_dump(
            include={
                "item_id",
                "tenant_id",
                "day",
                "run_id",
                "step_id",
                "action_hash",
                "reason",
                "key_id",
                "created_at",
            }
        )
        with connection_for(self._bind) as conn:
            conn.execute(_INSERT, params)
        return self.get(item.tenant_id, item.item_id)

    def get(self, tenant_id: str, item_id: str) -> ReviewItem:
        if not _uuid(tenant_id):
            raise ReviewItemNotFoundError(f"no review item {item_id}")
        with connection_for(self._bind) as conn:
            row = (
                conn.execute(_GET, {"item_id": item_id, "tenant_id": tenant_id}).mappings().first()
            )
        if row is None:
            raise ReviewItemNotFoundError(f"no review item {item_id}")
        return _item(row)

    def list(
        self, tenant_id: str, *, status: Status | None = None, limit: int = 200
    ) -> list[ReviewItem]:
        if not _uuid(tenant_id):
            return []
        params = {"tenant_id": tenant_id, "status": status, "limit": max(0, limit)}
        with connection_for(self._bind) as conn:
            rows = conn.execute(_LIST, params).mappings().all()
        return [_item(row) for row in rows]

    def set_event(self, tenant_id: str, item_id: str, seq: int) -> ReviewItem:
        with connection_for(self._bind) as conn:
            conn.execute(_SET_EVENT, {"item_id": item_id, "tenant_id": tenant_id, "seq": seq})
        return self.get(tenant_id, item_id)

    def decide(
        self,
        tenant_id: str,
        item_id: str,
        *,
        verdict: Verdict,
        decided_by: str,
        decided_at: datetime,
    ) -> ReviewItem:
        self.get(tenant_id, item_id)  # not found before already decided
        params = {
            "item_id": item_id,
            "tenant_id": tenant_id,
            "verdict": verdict,
            "decided_by": decided_by,
            "decided_at": decided_at,
        }
        with connection_for(self._bind) as conn:
            result = conn.execute(_DECIDE, params)
        if result.rowcount != 1:
            raise ReviewAlreadyDecidedError(f"review item {item_id} was already decided")
        return self.get(tenant_id, item_id)
