"""Undo records written before an action (M26).

For a write an app declares undoable, trusted code writes an undo record before the write's
dispatch intent, the same way a grant's expiry job is written (M16b): no dispatched undoable write
lacks one. The record holds what the write replaces (`prior`), the record revision the write was
bound to, and the subject it changed, all taken from the hashed, prepared arguments, never from
model text.

A record starts `pending` and is armed only when independent read-back agrees and reports the
record's revision after the write (`result_version`). Any other outcome (a mismatch, an unknown
outcome, nothing dispatched, a protocol failure) makes it `unavailable` for good, with the
reason. An undo is bound to `result_version`: the target accepts the reversal only while the
record still has that revision, so any later change, including one that changed it away and back,
refuses it.

Settling is idempotent and safe to repeat after a crash: it reads the job's stored result and
evidence, and a record that already left `pending` is never changed by it.
"""

import threading
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import (
    AwareDatetime,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.models import FrozenModel
from gawe.core.record_changes import RecordSnapshot

UndoState = Literal["pending", "armed", "unavailable", "requested", "used", "failed"]
# Read-back outcomes (job results) and why each leaves a record unavailable.
REASONS: Mapping[str, str] = {
    "disagreement": "readback_mismatch",
    "unknown": "outcome_unknown",
    "not_dispatched": "not_dispatched",
    "protocol_failure": "protocol_failure",
    "recipient_changed": "not_dispatched",
}


class UndoConflictError(Exception):
    """A revision or immutable identity no longer matches."""


class UndoRecord(FrozenModel):
    undo_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    tenant_id: str
    run_id: str
    step_id: str
    tool: str
    tool_version: str
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_key: str = Field(min_length=1, max_length=256)
    subject: dict[str, str]
    prior: dict[str, str] | None
    bound_version: str = Field(min_length=1, max_length=128)
    created_at: AwareDatetime
    snapshots: tuple[RecordSnapshot, ...] = Field(default=(), max_length=200)
    state: UndoState = "pending"
    result_version: str | None = Field(default=None, min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=64)
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _original_snapshots(self) -> "UndoRecord":
        targets = {(s.resource, s.subject) for s in self.snapshots}
        if len(targets) != len(self.snapshots) or any(
            s.basis != "preparation" or s.revision != self.bound_version for s in self.snapshots
        ):
            raise ValueError("undo snapshots must describe the original bound revision")
        return self

    @model_serializer(mode="wrap")
    def _omit_absent_snapshots(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.snapshots:
            data.pop("snapshots", None)
        return data


# Fields fixed when the record is written; only state, result_version, reason and revision move.
IMMUTABLE = (
    "undo_id",
    "tenant_id",
    "run_id",
    "step_id",
    "tool",
    "tool_version",
    "action_hash",
    "operation_key",
    "subject",
    "prior",
    "bound_version",
    "created_at",
    "snapshots",
)


def validate_change(old: UndoRecord, new: UndoRecord) -> None:
    if any(getattr(old, k) != getattr(new, k) for k in IMMUTABLE):
        raise UndoConflictError("an undo record's identity can't change")
    if new.revision != old.revision + 1:
        raise UndoConflictError("an undo record moves one revision at a time")


class UndoStore(Protocol):
    def add(self, record: UndoRecord) -> bool:
        """Insert if absent. Adding the same record again is a no-op (False); a different record
        under the same id is a conflict."""
        ...

    def get(self, tenant_id: str, undo_id: str) -> UndoRecord | None: ...

    def for_run(self, tenant_id: str, run_id: str) -> list[UndoRecord]: ...

    def counts(self, tenant_id: str) -> dict[tuple[str, str | None], int]:
        """Snapshot counts by state and reason, without loading subject or prior content."""
        ...

    def replace(self, record: UndoRecord, *, expected_revision: int) -> UndoRecord: ...


class InMemoryUndoStore:
    def __init__(self) -> None:
        self._records: dict[str, UndoRecord] = {}
        self._lock = threading.Lock()

    def add(self, record: UndoRecord) -> bool:
        with self._lock:
            old = self._records.get(record.undo_id)
            if old is None:
                self._records[record.undo_id] = record
                return True
            if any(getattr(old, k) != getattr(record, k) for k in IMMUTABLE):
                raise UndoConflictError("conflicting undo record")
            return False

    def get(self, tenant_id: str, undo_id: str) -> UndoRecord | None:
        record = self._records.get(undo_id)
        return record if record is not None and record.tenant_id == tenant_id else None

    def for_run(self, tenant_id: str, run_id: str) -> list[UndoRecord]:
        return sorted(
            (r for r in self._records.values() if r.tenant_id == tenant_id and r.run_id == run_id),
            key=lambda r: r.undo_id,
        )

    def counts(self, tenant_id: str) -> dict[tuple[str, str | None], int]:
        with self._lock:
            return dict(
                Counter(
                    (r.state, r.reason) for r in self._records.values() if r.tenant_id == tenant_id
                )
            )

    def replace(self, record: UndoRecord, *, expected_revision: int) -> UndoRecord:
        with self._lock:
            old = self._records.get(record.undo_id)
            if old is None or old.revision != expected_revision:
                raise UndoConflictError("undo record changed")
            validate_change(old, record)
            self._records[record.undo_id] = record
            return record


@dataclass(frozen=True)
class Undoable:
    """What an app declares for an undoable (tool, version): how to read, from the prepared
    arguments, the subject the write changes, the state it replaces (None when there was none)
    and the record revision it was bound to."""

    subject: Callable[[Mapping[str, Any]], dict[str, str]]
    prior: Callable[[Mapping[str, Any]], dict[str, str] | None]
    bound_version: Callable[[Mapping[str, Any]], str]
    restore_tool: str = "crm.restore_assessment"
    snapshots: Callable[[Mapping[str, Any]], tuple[RecordSnapshot, ...]] = lambda args: ()


def undo_identity(tenant_id: str, run_id: str, step_id: str, action_hash: str) -> str:
    return sha256_hex(
        canonical_json(
            {
                "domain": "gawe.undo_record.v1",
                "tenant_id": tenant_id,
                "run_id": run_id,
                "step_id": step_id,
                "action_hash": action_hash,
            }
        )
    )


def settled(record: UndoRecord, outcome: str, facts: Mapping[str, Any]) -> UndoRecord | None:
    """The record after the write's outcome, or None when there is nothing to change: a record
    that already left `pending` keeps its state."""
    if record.state != "pending":
        return None
    changes: dict[str, Any]
    if outcome == "agreement":
        version = facts.get("result_version")
        if isinstance(version, str) and 0 < len(version) <= 128:
            changes = {"state": "armed", "result_version": version}
        else:
            changes = {"state": "unavailable", "reason": "result_version_unobserved"}
    else:
        changes = {"state": "unavailable", "reason": REASONS.get(outcome, "outcome_unknown")}
    return UndoRecord(**{**record.model_dump(), **changes, "revision": record.revision + 1})


def legacy_restore_version(tool: str) -> str:
    """M26 persisted requests/plans omitted a version; their only restore was 1.0.0.

    Never fall back to the registry's latest version: an upgrade cannot change an old request.
    Unknown unversioned trusted tools fail closed.
    """
    if tool != "crm.restore_assessment":
        raise ValueError("unversioned restore tool has no legacy version")
    return "1.0.0"


def reversal_key(operation_key: str) -> str:
    return sha256_hex(canonical_json({"domain": "gawe.undo.v1", "operation_key": operation_key}))


def reversal_run_id(record: UndoRecord) -> str:
    from uuid import UUID

    return str(UUID(reversal_key(record.operation_key)[:32]))


def restore_args(record: UndoRecord, *, include_snapshots: bool = False) -> dict[str, Any]:
    if record.result_version is None:
        raise UndoConflictError("undo record has no result revision")
    if record.prior is not None and record.subject.keys() & record.prior.keys():
        raise UndoConflictError("undo subject and prior overlap")
    args: dict[str, Any] = {
        **record.subject,
        **({"risk_level": "none", "summary": ""} if record.prior is None else record.prior),
        "record": {"contract_version": record.result_version},
        "undo_id": record.undo_id,
    }

    if include_snapshots:
        args["snapshots"] = [
            RecordSnapshot(
                **{
                    **s.model_dump(),
                    "basis": "confirmed_original",
                    "source_action_hash": record.action_hash,
                }
            ).model_dump(mode="json")
            for s in record.snapshots
        ]
    return args
