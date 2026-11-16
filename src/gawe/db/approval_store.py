"""Postgres implementation of ApprovalStore.

The database enforces the same rules the service checks: at most one open approval per step
(a partial unique index), a decision is all-or-nothing, and the decider differs from the
requester. A decision is applied with `WHERE decision IS NULL`, so racing deciders cannot both win.
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from gawe.core.approvals import (
    ApprovalAlreadyDecidedError,
    ApprovalAlreadyOpenError,
    ApprovalNotFoundError,
    ApprovalRecord,
    ApprovedCall,
    Decision,
)
from gawe.core.records import StepNotFoundError
from gawe.db.bind import Bind, connection_for

_UNIQUE_VIOLATION = "23505"
_ONE_OPEN_INDEX = "approvals_one_pending_per_step"

_COLUMNS = (
    "CAST(a.id AS text) AS approval_id, CAST(s.run_id AS text) AS run_id, s.step_key AS step_id, "
    "a.action_hash, a.approver_role, CAST(a.requested_by AS text) AS requested_by, "
    "a.requested_at, a.expires_at, CAST(a.decided_by AS text) AS decided_by, a.decision, "
    "a.comment, a.decided_at, a.preparation, a.resolved_args"
)
_FROM = "FROM approvals a JOIN steps s ON s.id = a.step_id"
_CURRENT_STEP = (
    "JOIN runs r ON r.id = s.run_id "
    "JOIN plans p ON p.id = s.plan_id AND p.version = r.current_plan_version"
)

_GET = text(f"SELECT {_COLUMNS} {_FROM} WHERE a.id = CAST(:id AS uuid)")
_GET_LOCKED = text(f"SELECT {_COLUMNS} {_FROM} WHERE a.id = CAST(:id AS uuid) FOR UPDATE OF a")
_FOR_STEP = text(
    f"SELECT {_COLUMNS} {_FROM} {_CURRENT_STEP} "
    "WHERE s.run_id = CAST(:run_id AS uuid) AND s.step_key = :step_key ORDER BY a.requested_at"
)
_LATEST_APPROVED = text(
    f"SELECT {_COLUMNS}, s.tool_version {_FROM} JOIN runs r ON r.id = s.run_id "
    "WHERE r.tenant_id = CAST(:tenant_id AS uuid) AND s.tool_name = :tool "
    "AND a.decision = 'approved' AND a.id <> CAST(:exclude_id AS uuid) "
    "AND (CAST(:before AS timestamptz) IS NULL OR a.decided_at < CAST(:before AS timestamptz)) "
    "ORDER BY a.decided_at DESC, a.id DESC LIMIT 1"
)
_OPEN = text(f"SELECT {_COLUMNS} {_FROM} WHERE a.decision IS NULL ORDER BY a.requested_at")
_STEP_ID = text(
    f"SELECT s.id FROM steps s {_CURRENT_STEP} "
    "WHERE s.run_id = CAST(:run_id AS uuid) AND s.step_key = :step_key"
)
_INSERT = text(
    "INSERT INTO approvals (id, step_id, action_hash, approver_role, requested_by, requested_at, "
    "expires_at, preparation, resolved_args) VALUES (CAST(:id AS uuid), "
    "CAST(:step_id AS uuid), :action_hash, :approver_role, "
    "CAST(:requested_by AS uuid), :requested_at, :expires_at, "
    "CAST(:preparation AS jsonb), CAST(:resolved_args AS jsonb))"
)
_DECIDE = text(
    "UPDATE approvals SET decided_by = CAST(:decided_by AS uuid), decision = :decision, "
    "comment = :comment, decided_at = :decided_at "
    "WHERE id = CAST(:id AS uuid) AND decision IS NULL"
)


def _to_record(row: Any) -> ApprovalRecord:
    return ApprovalRecord(**dict(row))


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


class PostgresApprovalStore:
    """Pass a Connection to join the caller's transaction, or an Engine to commit per call."""

    def __init__(self, bind: Bind) -> None:
        self._bind = bind

    def add(self, record: ApprovalRecord) -> None:
        with connection_for(self._bind) as conn:
            step_id = conn.execute(
                _STEP_ID, {"run_id": record.run_id, "step_key": record.step_id}
            ).scalar()
            if step_id is None:
                raise StepNotFoundError(f"no step {record.step_id} in run {record.run_id}")
            params = {
                "id": record.approval_id,
                "step_id": step_id,
                "action_hash": record.action_hash,
                "approver_role": record.approver_role,
                "requested_by": record.requested_by,
                "requested_at": record.requested_at,
                "expires_at": record.expires_at,
                "preparation": json.dumps(record.preparation),
                "resolved_args": json.dumps(record.resolved_args),
            }
            try:
                with conn.begin_nested():
                    conn.execute(_INSERT, params)
            except DBAPIError as exc:
                diag = getattr(exc.orig, "diag", None)
                if (
                    getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION
                    and getattr(diag, "constraint_name", None) == _ONE_OPEN_INDEX
                ):
                    raise ApprovalAlreadyOpenError(
                        f"step {record.step_id} already has an open approval"
                    ) from exc
                raise

    def get(self, approval_id: str) -> ApprovalRecord:
        if not _is_uuid(approval_id):
            raise ApprovalNotFoundError(f"no approval {approval_id}")
        with connection_for(self._bind) as conn:
            row = conn.execute(_GET, {"id": approval_id}).mappings().first()
        if row is None:
            raise ApprovalNotFoundError(f"no approval {approval_id}")
        return _to_record(row)

    def for_step(self, run_id: str, step_id: str) -> list[ApprovalRecord]:
        with connection_for(self._bind) as conn:
            rows = conn.execute(_FOR_STEP, {"run_id": run_id, "step_key": step_id}).mappings().all()
        return [_to_record(row) for row in rows]

    def open_approvals(self) -> list[ApprovalRecord]:
        with connection_for(self._bind) as conn:
            rows = conn.execute(_OPEN).mappings().all()
        return [_to_record(row) for row in rows]

    def latest_approved(
        self,
        *,
        tenant_id: str,
        tool: str,
        exclude_id: str,
        decided_before: datetime | None,
    ) -> ApprovedCall | None:
        if not (_is_uuid(tenant_id) and _is_uuid(exclude_id)):
            return None
        params = {
            "tenant_id": tenant_id,
            "tool": tool,
            "exclude_id": exclude_id,
            "before": decided_before,
        }
        with connection_for(self._bind) as conn:
            row = conn.execute(_LATEST_APPROVED, params).mappings().first()
        if row is None:
            return None
        fields = dict(row)
        version = fields.pop("tool_version")
        return ApprovedCall(ApprovalRecord(**fields), version)

    def record_decision(
        self,
        approval_id: str,
        *,
        decided_by: str,
        decision: Decision,
        comment: str | None,
        decided_at: datetime,
    ) -> ApprovalRecord:
        if not _is_uuid(approval_id):
            raise ApprovalNotFoundError(f"no approval {approval_id}")
        with connection_for(self._bind) as conn:
            row = conn.execute(_GET_LOCKED, {"id": approval_id}).mappings().first()
            if row is None:
                raise ApprovalNotFoundError(f"no approval {approval_id}")
            current = _to_record(row)
            if not current.is_open:
                raise ApprovalAlreadyDecidedError(f"approval {approval_id} was already decided")
            decided = ApprovalRecord(
                **{
                    **current.model_dump(),
                    "decided_by": decided_by,
                    "decision": decision,
                    "comment": comment,
                    "decided_at": decided_at,
                }
            )
            result = conn.execute(
                _DECIDE,
                {
                    "id": approval_id,
                    "decided_by": decided_by,
                    "decision": decision,
                    "comment": comment,
                    "decided_at": decided_at,
                },
            )
            if result.rowcount != 1:
                raise ApprovalAlreadyDecidedError(f"approval {approval_id} was already decided")
            return decided
