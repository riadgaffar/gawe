"""D5 observations are tenant-scoped count snapshots with bounded, content-free labels."""

from pathlib import Path

import pytest
from tests.unit.test_undo_runs import approve, rated
from tests.unit.test_write_readback import ALICE

from gawe.core.telemetry import InMemoryTelemetry


def snapshot(t: InMemoryTelemetry) -> dict[tuple[str, str, str], float]:
    return {
        (str(a["tenant_id"]), str(a["state"]), str(a["reason"])): n
        for name, n, a in t.observations
        if name == "gawe.undo.records"
    }


def test_snapshot_moves_from_armed_through_request_to_used(tmp_path: Path) -> None:
    e = rated(tmp_path, "low")
    t = InMemoryTelemetry()
    assert e.service._undo is not None
    e.service._undo.telemetry = t
    e.service.maintenance_runner.tick()
    assert snapshot(t)["t1", "armed", ""] == 1
    undone = e.service.request_undo(ALICE, e.run_id, "write")
    e.service.maintenance_runner.tick()
    assert snapshot(t)["t1", "armed", ""] == 0
    assert snapshot(t)["t1", "used", ""] == 0
    approve(e, undone.run_id)
    for _ in range(2):
        e.service.maintenance_runner.tick()
        assert snapshot(t)["t1", "used", ""] == 1  # a retry never increments the snapshot
    assert all(set(a) == {"tenant_id", "state", "reason"} for _, _, a in t.observations)


def test_counts_are_isolated_and_reasons_never_export_arbitrary_text(tmp_path: Path) -> None:
    e = rated(tmp_path)
    t = InMemoryTelemetry()
    assert e.service._undo is not None
    e.service._undo.telemetry = t
    e.orch.start_run(
        tenant_id="t2",
        acting_user_id="u",
        roles=("case_manager",),
        objective="private",
        profile="contract_review",
    )
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    e.stores.undo.replace(
        record.model_copy(
            update={
                "state": "failed",
                "reason": "private rating summary",
                "revision": record.revision + 1,
            }
        ),
        expected_revision=record.revision,
    )
    e.service.maintenance_runner.tick()
    counts = snapshot(t)
    assert counts["t1", "unavailable", "other"] == 1
    assert all(n == 0 for (tenant, _, _), n in counts.items() if tenant == "t2")
    assert "private" not in repr(t.observations)


def test_unreadable_counts_report_error_without_zeroes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = rated(tmp_path)
    t = InMemoryTelemetry()
    assert e.service._undo is not None
    e.service._undo.telemetry = t

    def unreadable(_: str) -> dict[tuple[str, str | None], int]:
        raise OSError("unreadable")

    monkeypatch.setattr(e.stores.undo, "counts", unreadable)
    beats = e.service.maintenance_runner.tick()
    assert all(b.status == "error" for b in beats)
    assert not t.observations
