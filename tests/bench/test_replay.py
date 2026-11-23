"""M25 S4: the event replay app's scripted cases (`python -m bench replay`).

Each case's expected record is written by hand in bench/replay/cases.py; these tests run them,
check the command writes every record, and that a changed outcome is reported, not absorbed.
"""

import io
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bench.cli import main
from bench.harness import Deps
from bench.replay.cases import CASES, differences, expected_record, run_case, run_suite

BY_ID = {c.case_id: c for c in CASES}


def _deps(tmp_path: Path) -> Deps:
    return Deps(
        results_dir=tmp_path,
        now=lambda: datetime(2026, 11, 23, 12, tzinfo=UTC),
        git=lambda: ("abc123", True),
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_each_case_matches_its_expected_record(case: object) -> None:
    assert differences(run_case(case), expected_record(case)) == {}  # type: ignore[arg-type]


def test_no_case_bills_twice_crosses_tenants_or_passes_the_limit() -> None:
    for case in CASES:
        record = run_case(case)
        assert record["other_tenant_delivered"] is False, case.case_id
        assert record["daily_total"] <= case.daily_limit, case.case_id
        assert len(record["replayed"]) == len(set(record["replayed"])), case.case_id
        assert not set(record["replayed"]) & set(record["delivered_by_original"]), case.case_id


def test_the_cases_cover_what_the_readers_asked_for() -> None:
    assert set(BY_ID) == {
        "rp-cross-tenant",
        "rp-tenant-in-plan",
        "rp-widened-filters",
        "rp-injected-payload",
        "rp-over-daily-limit",
        "rp-same-events-twice",
        "rp-a-b-c",
        "rp-late-callback",
        "rp-lost-reply",
    }


def test_replay_command_writes_every_record(tmp_path: Path) -> None:
    out = io.StringIO()
    assert main(["replay"], out=out, deps=_deps(tmp_path)) == 0
    directory = tmp_path / "20261123T120000Z-replay"
    trials = [json.loads(line) for line in (directory / "trials.jsonl").read_text().splitlines()]
    assert [t["case"] for t in trials] == [c.case_id for c in CASES]
    assert all(t["matches_expected"] and t["kind"] == "replay_case" for t in trials)
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["kind"] == "replay" and manifest["model"] is None
    assert f"{len(CASES)}/{len(CASES)} match their expected records" in out.getvalue()


def test_a_changed_outcome_is_reported(tmp_path: Path) -> None:
    case = BY_ID["rp-cross-tenant"]
    wrong = replace(case, expected={**case.expected, "error": None, "run_status": "completed"})
    out = io.StringIO()
    assert not run_suite(tmp_path, stamp="x", git=("abc", False), out=out, cases=[wrong])
    assert "MISMATCH" in out.getvalue() and "run_status" in out.getvalue()
