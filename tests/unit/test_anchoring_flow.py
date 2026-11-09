"""Anchoring wired into the engine: when it happens, what it covers, and what it never breaks."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.api_bundle import bundle
from tests.api_flow import ALICE, BOB
from tests.engine_flow import EMAIL_PLAN, INTERNAL_PLAN, EngineFlow, plan_of, step
from tests.helpers import signer

from gawe.anchoring.backfill import backfill
from gawe.anchoring.config import AnchorConfigError, sinks_from_env
from gawe.anchoring.file_sink import FileAnchorSink
from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, memory_stores
from gawe.core.anchor import Anchorer, AnchorRecord, AnchorSink, EvidenceCheck
from gawe.core.audit import AnchorPoint
from gawe.core.runs import RunRecord
from gawe.core.statemachine import RunStatus
from gawe.llm.fake import FakeProvider


class Hook:
    """Records each call and anchors the run once an Anchorer is attached."""

    def __init__(self) -> None:
        self.calls: list[RunRecord] = []
        self.anchorer: Anchorer | None = None

    def __call__(self, run: RunRecord) -> None:
        self.calls.append(run)
        if self.anchorer is not None:
            self.anchorer.anchor_run(run)


class ExternalSink(FileAnchorSink):
    name = "ext"
    external = True


class UnreachableSink:
    name = "down"
    external = True

    def put(self, record: AnchorRecord) -> AnchorRecord:
        raise ConnectionError("timestamp authority unreachable")

    def get(self, run_id: str) -> AnchorRecord | None:
        raise ConnectionError("unreachable")

    def check(self, record: AnchorRecord) -> EvidenceCheck:
        raise ConnectionError("unreachable")


def anchored_flow(tmp_path: Path, plans: list[Any]) -> tuple[EngineFlow, Hook, Anchorer]:
    hook = Hook()
    f = EngineFlow(plans, on_finished=hook)
    hook.anchorer = Anchorer(f.audit, [FileAnchorSink(tmp_path / "anchors.jsonl")])
    return f, hook, hook.anchorer


def _complete(f: EngineFlow) -> None:
    f.start()
    f.orch.advance("run1")


def _deny_by_policy(f: EngineFlow) -> None:
    f.start()
    f.orch.advance("run1")


def _reject(f: EngineFlow) -> None:
    f.start()
    f.orch.advance("run1")
    f.reject_open()
    f.orch.advance("run1")


def _fail_planning(f: EngineFlow) -> None:
    f.start()
    f.orch.advance("run1")


def _expire(f: EngineFlow) -> None:
    f.start()
    f.orch.advance("run1")
    f.clock.advance(hours=2)
    f.orch.advance("run1")


def _cancel_with_open_approval(f: EngineFlow) -> None:
    f.start()
    f.orch.advance("run1")
    f.orch.cancel("run1", by="alice")


BAD_PLAN = plan_of(step("s", "no.such_tool", {}))
DENIED_PLAN = plan_of(
    step("email", "contracts.send_email", {"to": "x@competitor.com", "body": "hi"})
)

ENDINGS: list[tuple[str, list[Any], Callable[[EngineFlow], None], RunStatus]] = [
    ("completed", [INTERNAL_PLAN], _complete, RunStatus.COMPLETED),
    ("policy_denied", [DENIED_PLAN], _deny_by_policy, RunStatus.DENIED),
    ("rejected", [EMAIL_PLAN], _reject, RunStatus.DENIED),
    ("planning_failed", [BAD_PLAN, BAD_PLAN], _fail_planning, RunStatus.FAILED),
    ("approval_expired", [EMAIL_PLAN], _expire, RunStatus.FAILED),
    ("cancelled", [EMAIL_PLAN], _cancel_with_open_approval, RunStatus.CANCELLED),
]


@pytest.mark.parametrize(
    ("plans", "drive", "status"),
    [e[1:] for e in ENDINGS],
    ids=[e[0] for e in ENDINGS],
)
def test_every_ending_anchors_the_last_event_once(
    tmp_path: Path,
    plans: list[Any],
    drive: Callable[[EngineFlow], None],
    status: RunStatus,
) -> None:
    f, hook, anchorer = anchored_flow(tmp_path, plans)
    drive(f)
    f.orch.advance("run1")  # driving a finished run again must not anchor it again
    assert [r.status for r in hook.calls] == [status]

    events = f.audit.events("run1")
    sink = FileAnchorSink(tmp_path / "anchors.jsonl")
    record = sink.get("run1")
    assert record is not None and record.run_status == status.value
    assert record.point == AnchorPoint(seq=events[-1].seq, head=events[-1].hash)

    check = anchorer.verify(hook.calls[0])
    assert check.status == "matched" and check.ok and check.chain.events_after_anchor == 0


def test_the_anchor_covers_the_approval_closed_by_the_run_ending(tmp_path: Path) -> None:
    f, _, _ = anchored_flow(tmp_path, [EMAIL_PLAN])
    _cancel_with_open_approval(f)
    record = FileAnchorSink(tmp_path / "anchors.jsonl").get("run1")
    assert record is not None
    assert f.audit_types()[-1] == "approval.closed"
    assert record.point.seq == len(f.audit.events("run1"))


def test_a_paused_run_is_not_anchored(tmp_path: Path) -> None:
    f, hook, _ = anchored_flow(tmp_path, [EMAIL_PLAN])
    f.start()
    assert f.orch.advance("run1").status is RunStatus.PAUSED_FOR_APPROVAL
    assert hook.calls == []
    assert FileAnchorSink(tmp_path / "anchors.jsonl").get("run1") is None


def test_a_hook_that_raises_never_changes_the_run(caplog: pytest.LogCaptureFixture) -> None:
    def explode(run: RunRecord) -> None:
        raise RuntimeError("anchor service crashed")

    f = EngineFlow([INTERNAL_PLAN], on_finished=explode)
    f.start()
    run = f.orch.advance("run1")
    assert run.status is RunStatus.COMPLETED
    assert f.audit_types()[-1] == "run.completed" and f.audit.verify("run1").ok
    assert "on_finished hook failed for run run1" in caplog.text


def test_an_unreachable_sink_does_not_stop_the_others(tmp_path: Path) -> None:
    hook = Hook()
    f = EngineFlow([INTERNAL_PLAN], on_finished=hook)
    hook.anchorer = Anchorer(f.audit, [UnreachableSink(), FileAnchorSink(tmp_path / "a.jsonl")])
    _complete(f)
    assert f.runs.get("run1").status is RunStatus.COMPLETED
    assert FileAnchorSink(tmp_path / "a.jsonl").get("run1") is not None


# ---------------------------------------------------------------------- wiring


def test_assemble_anchors_runs_when_sinks_are_given(tmp_path: Path) -> None:
    b, _ = bundle()
    sink = FileAnchorSink(tmp_path / "a.jsonl")
    service = assemble(
        b,
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider([EMAIL_PLAN]),
        background=InlineBackground(),
        anchor_sinks=[sink],
    )
    run = service.start_run(ALICE, objective="email counsel", profile="contract_review")
    assert sink.get(run.run_id) is None  # paused for approval
    (item,) = service.inbox(BOB)
    service.decide(BOB, item.approval_id, decision="approved", action_hash=item.action_hash)
    record = sink.get(run.run_id)
    assert record is not None and record.run_status == "completed"


def test_assemble_without_sinks_writes_nothing(tmp_path: Path) -> None:
    b, _ = bundle()
    service = assemble(
        b,
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider([INTERNAL_PLAN]),
        background=InlineBackground(),
    )
    run = service.start_run(ALICE, objective="review", profile="contract_review")
    assert service.get_run(ALICE, run.run_id).status == "completed"
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------- config


def test_no_setting_means_no_anchoring() -> None:
    assert sinks_from_env({}) == []
    assert sinks_from_env({"GAWE_ANCHOR_SINKS": " , "}) == []


def test_file_sink_goes_in_the_anchor_dir(tmp_path: Path) -> None:
    (sink,) = sinks_from_env({"GAWE_ANCHOR_SINKS": "File", "GAWE_ANCHOR_DIR": str(tmp_path)})
    assert isinstance(sink, FileAnchorSink)
    assert sink._path == tmp_path / "anchors.jsonl"


def test_default_anchor_dir() -> None:
    (sink,) = sinks_from_env({"GAWE_ANCHOR_SINKS": "file"})
    assert isinstance(sink, FileAnchorSink) and sink._path == Path(".anchors/anchors.jsonl")


@pytest.mark.parametrize("value", ["file,file", "s3", "rfc3161,rfc3161"])
def test_bad_sink_settings_are_refused(value: str) -> None:
    with pytest.raises(AnchorConfigError):
        sinks_from_env({"GAWE_ANCHOR_SINKS": value})


# -------------------------------------------------------------------- backfill


def finished_runs(f: EngineFlow, count: int) -> list[str]:
    ids = [f"run{i}" for i in range(1, count + 1)]
    for run_id in ids:
        f.start(run_id=run_id)
        f.orch.advance(run_id)
    return ids


def backfill_flow(count: int = 2) -> tuple[EngineFlow, list[str]]:
    f = EngineFlow([INTERNAL_PLAN] * count)
    return f, finished_runs(f, count)


def test_backfill_anchors_finished_runs_and_is_safe_to_repeat(tmp_path: Path) -> None:
    f, ids = backfill_flow()
    f.start(run_id="paused")  # not finished: never anchored
    sink = FileAnchorSink(tmp_path / "a.jsonl")
    anchorer = Anchorer(f.audit, [sink])
    assert backfill(f.runs, anchorer) == ids
    assert all(sink.get(i) is not None for i in ids) and sink.get("paused") is None
    assert backfill(f.runs, anchorer) == []


def test_backfill_gives_a_missing_sink_the_point_the_others_already_hold(tmp_path: Path) -> None:
    f, _ = backfill_flow(1)
    file_sink, ext = FileAnchorSink(tmp_path / "f.jsonl"), ExternalSink(tmp_path / "e.jsonl")
    Anchorer(f.audit, [file_sink]).anchor_run(f.runs.get("run1"))
    f.audit.record("run1", "approval.decided", "bob")  # lands after the anchor
    sinks: list[AnchorSink] = [file_sink, ext]
    assert backfill(f.runs, Anchorer(f.audit, sinks)) == ["run1"]
    held, filled = file_sink.get("run1"), ext.get("run1")
    assert held is not None and filled is not None
    assert filled.point == held.point and filled.digest == held.digest


def test_backfill_carries_on_past_a_failing_sink_and_run(tmp_path: Path) -> None:
    f, ids = backfill_flow()
    sink = FileAnchorSink(tmp_path / "a.jsonl")
    assert backfill(f.runs, Anchorer(f.audit, [UnreachableSink(), sink])) == ids

    class Exploding(Anchorer):
        def fill_missing(self, run: RunRecord) -> list[AnchorRecord]:
            if run.run_id == "run1":
                raise RuntimeError("audit store down")
            return super().fill_missing(run)

    other = FileAnchorSink(tmp_path / "b.jsonl")
    assert backfill(f.runs, Exploding(f.audit, [other])) == ["run2"]


def test_backfill_respects_the_limit(tmp_path: Path) -> None:
    f, _ = backfill_flow(3)
    assert backfill(f.runs, Anchorer(f.audit, [FileAnchorSink(tmp_path / "a.jsonl")]), limit=2) == [
        "run1",
        "run2",
    ]
