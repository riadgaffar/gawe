"""Independent expected records, exact comparison, separate artifacts and failing CLI status."""

import io
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bench.cli import EXIT_STOPPED, main
from bench.harness import Deps
from bench.integrity.scenarios import SCENARIOS as INTEGRITY
from bench.integrity.undo import SCENARIOS as UNDO
from bench.record_changes import CASES, KIND, Case, differences, expected_record, observe, run_suite
from bench.replay.cases import CASES as REPLAY


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_record_change_case_matches_literal_oracle(case: Case, tmp_path: Path) -> None:
    assert differences(observe(case, tmp_path), expected_record(case)) == {}


def test_record_change_command_writes_separate_zero_cost_artifacts(tmp_path: Path) -> None:
    out = io.StringIO()
    deps = Deps(
        results_dir=tmp_path,
        now=lambda: datetime(2026, 11, 26, tzinfo=UTC),
        git=lambda: ("abc", True),
    )
    assert main(["record-changes"], out=out, deps=deps) == 0
    directory = tmp_path / "20261126T000000Z-record-changes"
    rows = [json.loads(line) for line in (directory / "trials.jsonl").read_text().splitlines()]
    assert [row["case"] for row in rows] == [case.case_id for case in CASES]
    assert len(rows) == 18 and all(row["kind"] == KIND and row["matches_expected"] for row in rows)
    assert json.loads((directory / "manifest.json").read_text())["model"] is None
    status = json.loads((directory / "status.json").read_text())
    assert status["spent_usd"] == "0" and status["mismatched"] == 0 and status["complete"]
    assert "18/18" in out.getvalue()
    assert (len(INTEGRITY), len(UNDO), len(REPLAY)) == (23, 4, 9)


@pytest.mark.parametrize(
    "actual,wanted", [(True, 1), (1, 1.0), ({"a": None}, {}), ([1, 2], [2, 1])]
)
def test_oracle_preserves_types_keys_and_array_order(actual, wanted) -> None:
    assert differences(actual, wanted)


def test_wrong_oracle_reports_failure_and_command_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = replace(CASES[0], expected={**CASES[0].expected, "effects": 1})
    out = io.StringIO()
    assert not run_suite(tmp_path, stamp="wrong", git=("abc", False), out=out, cases=[wrong])
    assert "MISMATCH" in out.getvalue() and "effects" in out.getvalue()
    monkeypatch.setattr("bench.cli.run_record_changes_suite", lambda *args, **kwargs: False)
    assert (
        main(
            ["record-changes"], out=out, deps=Deps(results_dir=tmp_path, git=lambda: ("abc", True))
        )
        == EXIT_STOPPED
    )
