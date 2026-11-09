"""The verify route's anchor report, at the service layer."""

from pathlib import Path

import pytest
from tests.api_flow import ALICE, BOB, CAROL, ApiFlow
from tests.engine_flow import EMAIL_PLAN, INTERNAL_PLAN

from gawe.anchoring.file_sink import FileAnchorSink
from gawe.api.errors import BadRequestError, NotFoundError
from gawe.core.anchor import AnchorRecord, AnchorSink, EvidenceCheck
from gawe.core.audit import AnchorPoint


class RejectingSink(FileAnchorSink):
    name = "ext"
    external = True

    def check(self, record: AnchorRecord) -> EvidenceCheck:
        return EvidenceCheck(ok=False, external=True, reason="signature does not verify")


class DownSink:
    name = "down"
    external = True

    def put(self, record: AnchorRecord) -> AnchorRecord:
        raise ConnectionError("unreachable")

    def get(self, run_id: str) -> AnchorRecord | None:
        raise ConnectionError("unreachable")

    def check(self, record: AnchorRecord) -> EvidenceCheck:
        raise ConnectionError("unreachable")


def finished(tmp_path: Path, sink: AnchorSink | None = None) -> tuple[ApiFlow, str]:
    chosen = sink if sink is not None else FileAnchorSink(tmp_path / "anchors.jsonl")
    f = ApiFlow([INTERNAL_PLAN], anchor_sinks=[chosen])
    return f, f.start_as(ALICE)


def test_without_anchoring_the_status_is_none() -> None:
    f = ApiFlow([INTERNAL_PLAN])
    run_id = f.start_as(ALICE)
    verdict = f.service.verify_audit(ALICE, run_id)
    assert verdict.ok and verdict.anchor is not None and verdict.anchor.status == "none"
    assert verdict.anchor.source is None and verdict.anchor.seq is None


def test_a_finished_run_matches_its_stored_anchor(tmp_path: Path) -> None:
    f, run_id = finished(tmp_path)
    verdict = f.service.verify_audit(ALICE, run_id)
    anchor = verdict.anchor
    assert verdict.ok and anchor is not None
    assert (anchor.status, anchor.source, anchor.sink) == ("matched", "stored", "file")
    assert anchor.seq == verdict.checked and anchor.events_after_anchor == 0
    assert anchor.external is False and anchor.anchored_at is not None
    assert anchor.timestamped_at is None


def test_a_paused_run_has_no_anchor_yet(tmp_path: Path) -> None:
    f = ApiFlow([EMAIL_PLAN], anchor_sinks=[FileAnchorSink(tmp_path / "a.jsonl")])
    run_id = f.start_as(ALICE)
    verdict = f.service.verify_audit(ALICE, run_id)
    assert verdict.ok and verdict.anchor is not None and verdict.anchor.status == "none"


def test_events_after_the_anchor_are_reported_not_failed(tmp_path: Path) -> None:
    f, run_id = finished(tmp_path)
    f.audit.record(run_id, "approval.decided", "bob")
    verdict = f.service.verify_audit(ALICE, run_id)
    assert verdict.ok and verdict.anchor is not None
    assert verdict.anchor.status == "matched" and verdict.anchor.events_after_anchor == 1


def test_truncation_shows_as_a_mismatch(tmp_path: Path) -> None:
    f, run_id = finished(tmp_path)
    f.audit_store._events[run_id] = f.audit_store._events[run_id][:-1]
    verdict = f.service.verify_audit(ALICE, run_id)
    assert not verdict.ok and verdict.anchor is not None
    assert verdict.anchor.status == "mismatch"
    assert verdict.reason == "chain is shorter than the anchored point"


def test_evidence_that_fails_its_own_check_is_invalid(tmp_path: Path) -> None:
    f, run_id = finished(tmp_path, RejectingSink(tmp_path / "ext.jsonl"))
    verdict = f.service.verify_audit(ALICE, run_id)
    assert not verdict.ok and verdict.anchor is not None
    assert verdict.anchor.status == "evidence_invalid" and verdict.anchor.external is True


def test_an_unreachable_sink_leaves_the_chain_to_decide(tmp_path: Path) -> None:
    f, run_id = finished(tmp_path, DownSink())
    verdict = f.service.verify_audit(ALICE, run_id)
    assert verdict.ok and verdict.anchor is not None and verdict.anchor.status == "unavailable"


def test_an_auditors_own_copy_is_checked_instead(tmp_path: Path) -> None:
    f, run_id = finished(tmp_path)
    events = f.audit.events(run_id)
    good = f.service.verify_audit(ALICE, run_id, anchor=AnchorPoint(2, events[1].hash))
    assert good.ok and good.anchor is not None
    assert (good.anchor.status, good.anchor.source, good.anchor.seq) == ("matched", "caller", 2)
    assert good.anchor.sink is None and good.anchor.events_after_anchor == len(events) - 2

    bad = f.service.verify_audit(ALICE, run_id, anchor=AnchorPoint(2, events[0].hash))
    assert not bad.ok and bad.anchor is not None and bad.anchor.status == "mismatch"
    assert events[0].hash not in bad.model_dump_json()  # the sent hash is never echoed


def test_a_malformed_anchor_is_refused_without_echoing_it(tmp_path: Path) -> None:
    f, run_id = finished(tmp_path)
    with pytest.raises(BadRequestError) as caught:
        f.service.verify_audit(ALICE, run_id, anchor=AnchorPoint(1, "NOT-A-HASH"))
    assert "NOT-A-HASH" not in str(caught.value)


def test_another_tenant_still_gets_404_with_or_without_an_anchor(tmp_path: Path) -> None:
    f, run_id = finished(tmp_path)
    for anchor in (None, AnchorPoint(1, "a" * 64), AnchorPoint(1, "bad")):
        with pytest.raises(NotFoundError):
            f.service.verify_audit(CAROL, run_id, anchor=anchor)


def test_any_user_in_the_tenant_may_verify(tmp_path: Path) -> None:
    f, run_id = finished(tmp_path)
    verdict = f.service.verify_audit(BOB, run_id)
    assert verdict.anchor is not None and verdict.anchor.status == "matched"
