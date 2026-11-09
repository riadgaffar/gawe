"""External anchors for audit chain heads.

The hash chain detects any edit to a run's history except removal of its tail, and someone who
controls the database can rewrite every hash after an edit. An anchor closes that gap: when a run
ends, its last event's position and hash are copied to places the database owner does not
control (a sink). Verification then checks that the chain still reaches that point unchanged.

What a sink keeps is the record's `digest`: a hash over the tenant, run, sequence number and head.
Only that digest ever leaves the machine; run data never does.

Anchoring never fails a run. A sink that raises is logged and counted
(`gawe.anchor.failures`), and `scripts/anchor_backfill.py` anchors the runs it missed.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from gawe.core.audit import AnchorPoint, AuditLog, ChainVerification, anchor_mismatch, verify_chain
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.models import AuditEvent
from gawe.core.runs import RunRecord
from gawe.core.telemetry import NoopTelemetry, Telemetry

ANCHOR_SCHEMA = "gawe.anchor.v1"
_DOMAIN = b"gawe.anchor.v1\n"
_HEX = frozenset("0123456789abcdef")

log = logging.getLogger("gawe.anchor")

AnchorStatus = Literal["none", "matched", "mismatch", "evidence_invalid", "unavailable"]


class AnchorFormatError(ValueError):
    """A stored anchor record is not in the expected shape."""


class AnchorConflictError(Exception):
    """A sink already holds a different anchor for this run."""


def anchor_digest(*, tenant_id: str, run_id: str, point: AnchorPoint) -> str:
    """What a sink keeps and a timestamp authority signs."""
    body = {
        "schema": ANCHOR_SCHEMA,
        "tenant_id": tenant_id,
        "run_id": run_id,
        "seq": point.seq,
        "head": point.head,
    }
    return sha256_hex(_DOMAIN + canonical_json(body))


@dataclass(frozen=True)
class AnchorRecord:
    tenant_id: str
    run_id: str
    point: AnchorPoint
    run_status: str
    anchored_at: datetime
    digest: str
    sink: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schema: str = ANCHOR_SCHEMA

    def digest_ok(self) -> bool:
        expected = anchor_digest(tenant_id=self.tenant_id, run_id=self.run_id, point=self.point)
        return self.schema == ANCHOR_SCHEMA and self.digest == expected

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "seq": self.point.seq,
            "head": self.point.head,
            "run_status": self.run_status,
            "anchored_at": self.anchored_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "digest": self.digest,
            "sink": self.sink,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "AnchorRecord":
        try:
            strings = {
                k: data[k]
                for k in ("schema", "tenant_id", "run_id", "head", "run_status", "digest", "sink")
            }
            seq, evidence = data["seq"], data["evidence"]
            anchored_at = datetime.fromisoformat(data["anchored_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnchorFormatError("anchor record is missing or has malformed fields") from exc
        if not all(isinstance(v, str) for v in strings.values()):
            raise AnchorFormatError("anchor record fields must be strings")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise AnchorFormatError("anchor sequence number must be a positive integer")
        if not isinstance(evidence, dict) or anchored_at.tzinfo is None:
            raise AnchorFormatError("anchor record has malformed fields")
        if strings["schema"] != ANCHOR_SCHEMA:
            raise AnchorFormatError("unsupported anchor schema")
        return cls(
            tenant_id=strings["tenant_id"],
            run_id=strings["run_id"],
            point=AnchorPoint(seq=seq, head=strings["head"]),
            run_status=strings["run_status"],
            anchored_at=anchored_at,
            digest=strings["digest"],
            sink=strings["sink"],
            evidence=evidence,
        )


@dataclass(frozen=True)
class EvidenceCheck:
    """Whether a sink's evidence still vouches for a record's digest."""

    ok: bool
    external: bool
    timestamped_at: datetime | None = None
    reason: str | None = None


class AnchorSink(Protocol):
    name: str
    external: bool  # True only if someone other than us vouches for the record

    def put(self, record: AnchorRecord) -> AnchorRecord:
        """Keep `record` and return it with this sink's evidence. Idempotent for the same digest;
        raises AnchorConflictError if a different anchor exists for the run."""
        ...

    def get(self, run_id: str) -> AnchorRecord | None: ...

    def check(self, record: AnchorRecord) -> EvidenceCheck: ...


@dataclass(frozen=True)
class AnchorCheck:
    """The result of verifying a run's chain against its anchor."""

    status: AnchorStatus
    chain: ChainVerification
    source: Literal["stored", "caller"] | None = None
    record: AnchorRecord | None = None
    evidence: EvidenceCheck | None = None

    @property
    def ok(self) -> bool:
        """False if the chain is broken or an anchor contradicts it."""
        return self.chain.ok and self.status not in ("mismatch", "evidence_invalid")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _valid_point(point: AnchorPoint) -> bool:
    return point.seq >= 1 and len(point.head) == 64 and set(point.head) <= _HEX


class Anchorer:
    """Writes anchors when runs end and checks chains against them.

    Sinks are consulted external-first when reading, so a signed timestamp wins over the local
    file when both exist. With no sinks it writes nothing and can still check a chain against an
    auditor's own copy of an anchor.
    """

    def __init__(
        self,
        audit: AuditLog,
        sinks: Sequence[AnchorSink],
        *,
        telemetry: Telemetry | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._audit = audit
        self._sinks = sorted(sinks, key=lambda s: not s.external)
        self._telemetry: Telemetry = telemetry or NoopTelemetry()
        self._clock = clock

    def anchor_run(self, run: RunRecord) -> list[AnchorRecord]:
        """Anchor the run's current last event in every sink. Never raises for a sink failure."""
        return self._write_head(run, self._sinks)

    def fill_missing(self, run: RunRecord) -> list[AnchorRecord]:
        """For the backfill: anchor the run in the sinks that lack it.

        If some sink already holds an anchor, the others get that same point rather than the
        current head, so every sink agrees even when events were added after the run ended.
        """
        present: list[AnchorRecord] = []
        missing: list[AnchorSink] = []
        for sink in self._sinks:
            try:
                record = sink.get(run.run_id)
            except Exception as exc:
                self._failed(sink, run.run_id, exc)
                continue
            if record is None:
                missing.append(sink)
            else:
                present.append(record)
        if not missing:
            return []
        if present:
            return self._write(run, present[0].point, missing)
        return self._write_head(run, missing)

    def is_anchored(self, run_id: str) -> bool:
        """True if any sink holds an anchor for the run."""
        return any(sink.get(run_id) is not None for sink in self._sinks)

    def _write_head(self, run: RunRecord, sinks: Sequence[AnchorSink]) -> list[AnchorRecord]:
        last = self._audit.last(run.run_id)
        if last is None:
            return []
        return self._write(run, AnchorPoint(seq=last.seq, head=last.hash), sinks)

    def _write(
        self, run: RunRecord, point: AnchorPoint, sinks: Sequence[AnchorSink]
    ) -> list[AnchorRecord]:
        base = AnchorRecord(
            tenant_id=run.tenant_id,
            run_id=run.run_id,
            point=point,
            run_status=run.status.value,
            anchored_at=self._clock(),
            digest=anchor_digest(tenant_id=run.tenant_id, run_id=run.run_id, point=point),
            sink="",
        )
        written: list[AnchorRecord] = []
        for sink in sinks:
            try:
                written.append(sink.put(replace(base, sink=sink.name)))
            except Exception as exc:  # a sink must never break a run
                self._failed(sink, run.run_id, exc)
            else:
                self._telemetry.count("gawe.anchor.written", sink=sink.name)
        return written

    def _failed(self, sink: AnchorSink, run_id: str, exc: Exception) -> None:
        log.warning("anchor sink %s failed for run %s (%s)", sink.name, run_id, type(exc).__name__)
        self._telemetry.count("gawe.anchor.failures", sink=sink.name)

    def verify(self, run: RunRecord, *, caller: AnchorPoint | None = None) -> AnchorCheck:
        """Check the run's chain, against the caller's own anchor copy if given, else the stored
        anchor. Sinks that fail to answer give `unavailable`, never an exception."""
        events = self._audit.events(run.run_id)
        if caller is not None:
            if not _valid_point(caller):
                raise ValueError("anchor must be a positive sequence number and a SHA-256 hash")
            return _against(events, caller, source="caller")

        try:
            found = self._stored(run.run_id)
        except Exception as exc:
            log.warning("anchor lookup failed for run %s (%s)", run.run_id, type(exc).__name__)
            return AnchorCheck(status="unavailable", chain=verify_chain(events))
        if found is None:
            return AnchorCheck(status="none", chain=verify_chain(events))
        sink, record = found

        belongs = record.run_id == run.run_id and record.tenant_id == run.tenant_id
        if not belongs or not record.digest_ok():
            return AnchorCheck(
                status="evidence_invalid",
                chain=verify_chain(events),
                source="stored",
                record=record,
            )
        try:
            evidence = sink.check(record)
        except Exception as exc:
            log.warning("anchor check failed for run %s (%s)", run.run_id, type(exc).__name__)
            return AnchorCheck(
                status="unavailable", chain=verify_chain(events), source="stored", record=record
            )
        if not evidence.ok:
            return AnchorCheck(
                status="evidence_invalid",
                chain=verify_chain(events),
                source="stored",
                record=record,
                evidence=evidence,
            )
        result = _against(events, record.point, source="stored")
        return replace(result, record=record, evidence=evidence)

    def _stored(self, run_id: str) -> tuple[AnchorSink, AnchorRecord] | None:
        for sink in self._sinks:
            record = sink.get(run_id)
            if record is not None:
                return sink, record
        return None


def _against(
    events: Sequence[AuditEvent], point: AnchorPoint, *, source: Literal["stored", "caller"]
) -> AnchorCheck:
    # "matched" only describes the anchor; a chain broken elsewhere still makes `ok` false.
    status: AnchorStatus = "mismatch" if anchor_mismatch(events, point) is not None else "matched"
    return AnchorCheck(status=status, chain=verify_chain(events, anchor=point), source=source)
