"""Append-only, hash-chained audit log (one chain per run).

Each event's hash covers its own fields plus the previous event's hash, so editing, removing,
inserting or reordering any event breaks verification from that point on.

What the chain does NOT detect on its own: removal of the *tail* of a chain. Callers that need
that guarantee keep a copy of an event's sequence number and hash outside the database (an
`AnchorPoint`, see `gawe.core.anchor`) and pass it back as `anchor`. Passing the final head as
`expected_head` is the older, stricter form: it also fails when events were added after it.

Events carry only a hash of their payload. The payload itself lives in the artifact store, so
confidential contract text never enters the log.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.models import AuditEvent

GENESIS_HASH = "0" * 64
_DOMAIN = b"gawe.audit.v1\n"
_MAX_FIELD = 128


class ChainConflictError(Exception):
    """An event does not extend the current chain (a concurrent writer got there first)."""


class AuditStore(Protocol):
    def last(self, run_id: str) -> AuditEvent | None: ...

    def append(self, event: AuditEvent) -> None:
        """Atomically add `event`; raise ChainConflictError unless it extends the chain."""
        ...

    def events(self, run_id: str) -> list[AuditEvent]: ...


def _timestamp(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return ts.astimezone(UTC).isoformat(timespec="microseconds")


def compute_event_hash(
    *,
    seq: int,
    run_id: str,
    type: str,
    actor: str,
    payload_hash: str,
    prev_hash: str,
    ts: datetime,
) -> str:
    body = {
        "seq": seq,
        "run_id": run_id,
        "type": type,
        "actor": actor,
        "payload_hash": payload_hash,
        "prev_hash": prev_hash,
        "ts": _timestamp(ts),
    }
    return sha256_hex(_DOMAIN + canonical_json(body))


def _recompute(event: AuditEvent) -> str:
    return compute_event_hash(
        seq=event.seq,
        run_id=event.run_id,
        type=event.type,
        actor=event.actor,
        payload_hash=event.payload_hash,
        prev_hash=event.prev_hash,
        ts=event.ts,
    )


@dataclass(frozen=True)
class AnchorPoint:
    """A copy of one event's position and hash, kept outside the database."""

    seq: int
    head: str


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    checked: int
    head: str | None
    failed_seq: int | None = None
    reason: str | None = None
    anchored_seq: int | None = None
    events_after_anchor: int = 0


def _fail(checked: int, seq: int | None, reason: str) -> ChainVerification:
    return ChainVerification(ok=False, checked=checked, head=None, failed_seq=seq, reason=reason)


def anchor_mismatch(events: Sequence[AuditEvent], anchor: AnchorPoint) -> str | None:
    """Why `events` do not reach `anchor`, or None if the event at `anchor.seq` has its hash.

    Only the position is checked here; `verify_chain` checks the chain itself.
    """
    if anchor.seq < 1 or anchor.seq > len(events):
        return "chain is shorter than the anchored point"
    if events[anchor.seq - 1].hash != anchor.head:
        return "event at the anchored point does not match the anchor"
    return None


def verify_chain(
    events: Sequence[AuditEvent],
    *,
    expected_head: str | None = None,
    anchor: AnchorPoint | None = None,
) -> ChainVerification:
    """Check linkage, numbering and every hash. `events` must be in chain order.

    With `anchor`, the chain must also reach that point unchanged. Events after it are counted in
    `events_after_anchor`, not treated as a failure (a decision can land just after a run ends).
    """
    prev_hash = GENESIS_HASH
    run_id = events[0].run_id if events else None
    for index, event in enumerate(events):
        if event.run_id != run_id:
            return _fail(index, event.seq, "event belongs to a different run")
        if event.seq != index + 1:
            return _fail(index, event.seq, f"expected seq {index + 1}")
        if event.prev_hash != prev_hash:
            return _fail(index, event.seq, "prev_hash does not match the previous event")
        if _recompute(event) != event.hash:
            return _fail(index, event.seq, "hash does not match the event contents")
        prev_hash = event.hash

    head = events[-1].hash if events else None
    if expected_head is not None and head != expected_head:
        return _fail(len(events), None, "chain head does not match the anchored head")
    if anchor is None:
        return ChainVerification(ok=True, checked=len(events), head=head)
    problem = anchor_mismatch(events, anchor)
    if problem is not None:
        return _fail(len(events), None, problem)
    return ChainVerification(
        ok=True,
        checked=len(events),
        head=head,
        anchored_seq=anchor.seq,
        events_after_anchor=len(events) - anchor.seq,
    )


class InMemoryAuditStore:
    """Reference store with the same append contract as the Postgres trigger."""

    def __init__(self) -> None:
        self._events: dict[str, list[AuditEvent]] = {}
        self._lock = threading.Lock()

    def last(self, run_id: str) -> AuditEvent | None:
        with self._lock:
            chain = self._events.get(run_id)
            return chain[-1] if chain else None

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            chain = self._events.setdefault(event.run_id, [])
            expected_seq = len(chain) + 1
            expected_prev = chain[-1].hash if chain else GENESIS_HASH
            if event.seq != expected_seq or event.prev_hash != expected_prev:
                raise ChainConflictError(
                    f"event seq {event.seq} does not extend the chain (expected {expected_seq})"
                )
            chain.append(event)

    def events(self, run_id: str) -> list[AuditEvent]:
        with self._lock:
            return list(self._events.get(run_id, []))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditLog:
    def __init__(
        self,
        store: AuditStore,
        *,
        clock: Callable[[], datetime] = _utc_now,
        max_retries: int = 3,
    ) -> None:
        self._store = store
        self._clock = clock
        self._max_retries = max_retries

    def record(
        self,
        run_id: str,
        type: str,
        actor: str,
        payload: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        """Append an event. Only a hash of `payload` is stored in the log."""
        for name, value in (("run_id", run_id), ("type", type), ("actor", actor)):
            if not value or len(value) > _MAX_FIELD:
                raise ValueError(f"{name} must be 1-{_MAX_FIELD} characters")
        payload_hash = sha256_hex(canonical_json(payload if payload is not None else {}))

        for _ in range(self._max_retries + 1):
            last = self._store.last(run_id)
            seq = 1 if last is None else last.seq + 1
            prev_hash = GENESIS_HASH if last is None else last.hash
            ts = self._clock()
            event = AuditEvent(
                seq=seq,
                run_id=run_id,
                type=type,
                actor=actor,
                payload_hash=payload_hash,
                prev_hash=prev_hash,
                hash=compute_event_hash(
                    seq=seq,
                    run_id=run_id,
                    type=type,
                    actor=actor,
                    payload_hash=payload_hash,
                    prev_hash=prev_hash,
                    ts=ts,
                ),
                ts=ts,
            )
            try:
                self._store.append(event)
            except ChainConflictError:
                continue  # another writer extended the chain first; rebuild on the new head
            return event
        raise ChainConflictError(f"could not append to run {run_id} after retries")

    def events(self, run_id: str) -> list[AuditEvent]:
        return self._store.events(run_id)

    def last(self, run_id: str) -> AuditEvent | None:
        return self._store.last(run_id)

    def verify(
        self,
        run_id: str,
        *,
        expected_head: str | None = None,
        anchor: AnchorPoint | None = None,
    ) -> ChainVerification:
        return verify_chain(self._store.events(run_id), expected_head=expected_head, anchor=anchor)
