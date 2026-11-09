import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from gawe.anchoring.file_sink import FileAnchorSink
from gawe.core.anchor import (
    ANCHOR_SCHEMA,
    AnchorConflictError,
    Anchorer,
    AnchorFormatError,
    AnchorRecord,
    EvidenceCheck,
    anchor_digest,
)
from gawe.core.audit import AnchorPoint, AuditLog, InMemoryAuditStore
from gawe.core.runs import RunRecord
from gawe.core.statemachine import RunStatus
from gawe.core.telemetry import InMemoryTelemetry

T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
HEAD = "ab" * 32


def make_run(run_id: str = "run1", tenant_id: str = "t1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        tenant_id=tenant_id,
        acting_user_id="alice",
        objective="review",
        profile="contract_review",
        status=RunStatus.COMPLETED,
        created_at=T0,
    )


def make_log() -> tuple[AuditLog, InMemoryAuditStore]:
    store = InMemoryAuditStore()
    tick = count()
    return AuditLog(store, clock=lambda: T0 + timedelta(seconds=next(tick))), store


def make_record(**changes: Any) -> AnchorRecord:
    point = changes.pop("point", AnchorPoint(seq=3, head=HEAD))
    tenant_id, run_id = changes.pop("tenant_id", "t1"), changes.pop("run_id", "run1")
    record = AnchorRecord(
        tenant_id=tenant_id,
        run_id=run_id,
        point=point,
        run_status="completed",
        anchored_at=T0,
        digest=anchor_digest(tenant_id=tenant_id, run_id=run_id, point=point),
        sink="file",
    )
    return replace(record, **changes)


class BrokenSink:
    name = "broken"
    external = True

    def put(self, record: AnchorRecord) -> AnchorRecord:
        raise OSError("disk full")

    def get(self, run_id: str) -> AnchorRecord | None:
        raise OSError("disk gone")

    def check(self, record: AnchorRecord) -> EvidenceCheck:
        raise OSError("unreachable")


class ExternalSink(FileAnchorSink):
    """A file sink that claims to be external, to test which sink is read first."""

    name = "ext"
    external = True


def finished_run(log: AuditLog, events: int = 3, run_id: str = "run1") -> RunRecord:
    for i in range(events):
        log.record(run_id, f"type{i}", "gawe.orchestrator")
    return make_run(run_id)


# ------------------------------------------------------------------- the record


def test_digest_covers_tenant_run_seq_and_head() -> None:
    base: dict[str, Any] = {"tenant_id": "t1", "run_id": "run1", "point": AnchorPoint(3, HEAD)}
    digest = anchor_digest(**base)
    assert digest == anchor_digest(**base)
    for change in (
        {"tenant_id": "t2"},
        {"run_id": "run2"},
        {"point": AnchorPoint(4, HEAD)},
        {"point": AnchorPoint(3, "cd" * 32)},
    ):
        assert anchor_digest(**{**base, **change}) != digest


def test_record_round_trips_through_json() -> None:
    record = make_record(evidence={"tsa": "x"})
    restored = AnchorRecord.from_json(json.loads(json.dumps(record.to_json())))
    assert restored == record and restored.digest_ok()


def test_a_record_whose_fields_were_changed_fails_its_digest() -> None:
    assert not replace(make_record(), point=AnchorPoint(seq=4, head=HEAD)).digest_ok()
    assert not replace(make_record(), tenant_id="t2").digest_ok()


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "gawe.anchor.v0"},
        {"seq": 0},
        {"seq": True},
        {"seq": "3"},
        {"anchored_at": "2026-10-29T12:00:00"},  # no timezone
        {"evidence": []},
        {"digest": 5},
    ],
)
def test_malformed_records_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(AnchorFormatError):
        AnchorRecord.from_json({**make_record().to_json(), **change})


def test_a_record_with_a_missing_field_is_refused() -> None:
    data = make_record().to_json()
    del data["head"]
    with pytest.raises(AnchorFormatError):
        AnchorRecord.from_json(data)


# -------------------------------------------------------------------- file sink


def test_file_sink_keeps_a_record_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "anchors" / "anchors.jsonl"
    record = make_record()
    assert FileAnchorSink(path).put(record) == record
    reloaded = FileAnchorSink(path)
    assert reloaded.get("run1") == record
    assert reloaded.get("run2") is None
    assert reloaded.check(record).ok and not reloaded.check(record).external


def test_file_sink_put_is_idempotent_for_the_same_digest(tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "a.jsonl")
    first = make_record()
    sink.put(first)
    assert sink.put(replace(first, anchored_at=T0 + timedelta(hours=1))) == first
    assert len((tmp_path / "a.jsonl").read_text().splitlines()) == 1


def test_file_sink_refuses_a_second_different_anchor(tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "a.jsonl")
    sink.put(make_record())
    with pytest.raises(AnchorConflictError):
        sink.put(make_record(point=AnchorPoint(seq=4, head=HEAD)))


def test_file_sink_refuses_a_record_for_another_sink(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FileAnchorSink(tmp_path / "a.jsonl").put(make_record(sink="rfc3161"))


def test_file_sink_rejects_a_tampered_file(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    sink = FileAnchorSink(path)
    sink.put(make_record())
    second = make_record(point=AnchorPoint(seq=1, head=HEAD))
    with path.open("a") as handle:  # someone appends a competing anchor by hand
        handle.write(json.dumps(second.to_json()) + "\n")
    with pytest.raises(AnchorConflictError):
        sink.get("run1")
    path.write_text("not json\n")
    with pytest.raises(AnchorFormatError):
        sink.get("run1")
    path.write_text("[1, 2]\n")
    with pytest.raises(AnchorFormatError):
        sink.get("run1")


def test_file_sink_check_catches_a_bad_digest(tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "a.jsonl")
    assert not sink.check(make_record(digest="0" * 64)).ok


# ------------------------------------------------------------------- anchoring


def test_anchor_run_records_the_last_event_in_every_sink(tmp_path: Path) -> None:
    log, store = make_log()
    run = finished_run(log)
    telemetry = InMemoryTelemetry()
    anchorer = Anchorer(log, [FileAnchorSink(tmp_path / "a.jsonl")], telemetry=telemetry)
    [record] = anchorer.anchor_run(run)
    last = store.events("run1")[-1]
    assert record.point == AnchorPoint(seq=last.seq, head=last.hash)
    assert record.tenant_id == "t1" and record.run_status == "completed" and record.digest_ok()
    assert anchorer.is_anchored("run1") and not anchorer.is_anchored("run2")
    assert [c[0] for c in telemetry.counts] == ["gawe.anchor.written"]


def test_a_run_with_no_events_is_not_anchored(tmp_path: Path) -> None:
    log, _ = make_log()
    anchorer = Anchorer(log, [FileAnchorSink(tmp_path / "a.jsonl")])
    assert anchorer.anchor_run(make_run()) == []


def test_a_failing_sink_never_raises_and_is_counted(tmp_path: Path) -> None:
    log, store = make_log()
    run = finished_run(log)
    before = store.events("run1")
    telemetry = InMemoryTelemetry()
    file_sink = FileAnchorSink(tmp_path / "a.jsonl")
    anchorer = Anchorer(log, [BrokenSink(), file_sink], telemetry=telemetry)
    written = anchorer.anchor_run(run)
    assert [r.sink for r in written] == ["file"]
    assert store.events("run1") == before  # anchoring never touches the audit log
    assert ("gawe.anchor.failures", 1, {"sink": "broken"}) in telemetry.counts


def test_an_anchorer_without_sinks_writes_nothing_but_checks_a_callers_copy() -> None:
    log, _ = make_log()
    run = finished_run(log)
    anchorer = Anchorer(log, [])
    assert anchorer.anchor_run(run) == [] and not anchorer.is_anchored("run1")
    assert anchorer.verify(run).status == "none"
    head = log.events("run1")[-1]
    assert anchorer.verify(run, caller=AnchorPoint(head.seq, head.hash)).status == "matched"


# ------------------------------------------------------------------- verifying


def test_verify_without_an_anchor_reports_none(tmp_path: Path) -> None:
    log, _ = make_log()
    run = finished_run(log)
    check = Anchorer(log, [FileAnchorSink(tmp_path / "a.jsonl")]).verify(run)
    assert check.status == "none" and check.ok and check.record is None


def test_verify_matches_the_stored_anchor_and_counts_later_events(tmp_path: Path) -> None:
    log, _ = make_log()
    run = finished_run(log)
    anchorer = Anchorer(log, [FileAnchorSink(tmp_path / "a.jsonl")])
    anchorer.anchor_run(run)
    log.record("run1", "approval.decided", "bob")  # a decision that lands after the run ended
    check = anchorer.verify(run)
    assert check.status == "matched" and check.ok and check.source == "stored"
    assert check.chain.events_after_anchor == 1
    assert check.evidence is not None and not check.evidence.external


def test_verify_detects_truncation_against_the_stored_anchor(tmp_path: Path) -> None:
    log, store = make_log()
    run = finished_run(log, events=4)
    anchorer = Anchorer(log, [FileAnchorSink(tmp_path / "a.jsonl")])
    anchorer.anchor_run(run)
    store._events["run1"] = store._events["run1"][:2]  # the owner deletes the tail
    check = anchorer.verify(run)
    assert check.status == "mismatch" and not check.ok


def test_an_anchor_belonging_to_another_tenant_is_invalid(tmp_path: Path) -> None:
    log, _ = make_log()
    run = finished_run(log)
    sink = FileAnchorSink(tmp_path / "a.jsonl")
    last = log.last("run1")
    assert last is not None
    sink.put(make_record(tenant_id="t2", point=AnchorPoint(last.seq, last.hash)))
    check = Anchorer(log, [sink]).verify(run)
    assert check.status == "evidence_invalid" and not check.ok


def test_a_stored_anchor_with_a_bad_digest_is_invalid(tmp_path: Path) -> None:
    log, _ = make_log()
    run = finished_run(log)
    path = tmp_path / "a.jsonl"
    last = log.last("run1")
    assert last is not None
    forged = make_record(point=AnchorPoint(last.seq, last.hash), digest="0" * 64)
    path.write_text(json.dumps(forged.to_json()) + "\n")
    check = Anchorer(log, [FileAnchorSink(path)]).verify(run)
    assert check.status == "evidence_invalid" and not check.ok


def test_a_sink_that_cannot_answer_gives_unavailable(tmp_path: Path) -> None:
    log, _ = make_log()
    run = finished_run(log)
    check = Anchorer(log, [BrokenSink()]).verify(run)
    assert check.status == "unavailable" and check.ok  # the chain alone still decides


def test_the_external_sink_is_read_before_the_file(tmp_path: Path) -> None:
    log, _ = make_log()
    run = finished_run(log)
    file_sink, ext = FileAnchorSink(tmp_path / "f.jsonl"), ExternalSink(tmp_path / "e.jsonl")
    anchorer = Anchorer(log, [file_sink, ext])
    assert {r.sink for r in anchorer.anchor_run(run)} == {"file", "ext"}
    check = anchorer.verify(run)
    assert check.record is not None and check.record.sink == "ext"


def test_a_callers_own_copy_overrides_the_stored_anchor(tmp_path: Path) -> None:
    log, _ = make_log()
    run = finished_run(log, events=4)
    anchorer = Anchorer(log, [BrokenSink()])  # stored anchors unreachable; caller's copy is used
    events = log.events("run1")
    good = anchorer.verify(run, caller=AnchorPoint(seq=2, head=events[1].hash))
    assert good.status == "matched" and good.source == "caller" and good.ok
    bad = anchorer.verify(run, caller=AnchorPoint(seq=2, head=events[2].hash))
    assert bad.status == "mismatch" and not bad.ok


@pytest.mark.parametrize(
    "point", [AnchorPoint(0, HEAD), AnchorPoint(1, "xyz"), AnchorPoint(1, "A" * 64)]
)
def test_a_malformed_caller_anchor_is_refused(tmp_path: Path, point: AnchorPoint) -> None:
    log, _ = make_log()
    run = finished_run(log)
    with pytest.raises(ValueError):
        Anchorer(log, [FileAnchorSink(tmp_path / "a.jsonl")]).verify(run, caller=point)


def test_schema_constant_is_versioned() -> None:
    assert ANCHOR_SCHEMA == "gawe.anchor.v1"


class FailingCheckSink(FileAnchorSink):
    def check(self, record: AnchorRecord) -> EvidenceCheck:
        raise OSError("authority certificates unreadable")


class RejectingSink(FileAnchorSink):
    def check(self, record: AnchorRecord) -> EvidenceCheck:
        return EvidenceCheck(ok=False, external=True, reason="signature does not verify")


def test_a_sink_that_fails_to_check_its_evidence_gives_unavailable(tmp_path: Path) -> None:
    log, _ = make_log()
    run = finished_run(log)
    anchorer = Anchorer(log, [FailingCheckSink(tmp_path / "a.jsonl")])
    anchorer.anchor_run(run)
    check = anchorer.verify(run)
    assert check.status == "unavailable" and check.ok and check.record is not None


def test_evidence_that_does_not_hold_up_is_invalid(tmp_path: Path) -> None:
    log, _ = make_log()
    run = finished_run(log)
    anchorer = Anchorer(log, [RejectingSink(tmp_path / "a.jsonl")])
    anchorer.anchor_run(run)
    check = anchorer.verify(run)
    assert check.status == "evidence_invalid" and not check.ok
    assert check.evidence is not None and check.evidence.reason == "signature does not verify"


def test_blank_lines_in_the_anchor_file_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    record = make_record()
    path.write_text("\n" + json.dumps(record.to_json()) + "\n\n")
    assert FileAnchorSink(path).get("run1") == record
