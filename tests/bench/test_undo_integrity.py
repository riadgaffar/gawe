"""The undo group has its own denominator and literal oracle; mismatches fail the command."""

import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from bench.integrity.scenarios import SCENARIOS as ORIGINAL_SCENARIOS
from bench.integrity.undo import KIND, SCENARIOS, expected_record, observe, run_suite


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_undo_scenario_matches_independent_record(scenario, tmp_path: Path) -> None:
    assert observe(scenario, tmp_path) == expected_record(scenario)


def test_undo_group_is_separate_and_writes_its_own_artifacts(tmp_path: Path) -> None:
    assert len(ORIGINAL_SCENARIOS) == 23 and len(SCENARIOS) == 4
    assert not {s.scenario_id for s in SCENARIOS} & {s.scenario_id for s in ORIGINAL_SCENARIOS}
    out = io.StringIO()
    assert run_suite(tmp_path, stamp="test", git=("abc", True), out=out)
    directory = tmp_path / "test-undo-integrity"
    records = [json.loads(line) for line in (directory / "trials.jsonl").read_text().splitlines()]
    assert len(records) == 4 and all(r["kind"] == KIND and r["matches_expected"] for r in records)
    assert json.loads((directory / "manifest.json").read_text())["model"] is None
    assert "undo: 4/4" in out.getvalue()


def test_wrong_expectations_are_reported(tmp_path: Path) -> None:
    scenario = SCENARIOS[0]
    wrong = replace(scenario, expected={**scenario.expected, "accepted_restores": 1})
    out = io.StringIO()
    assert not run_suite(tmp_path, stamp="wrong", git=("abc", False), out=out, scenarios=[wrong])
    assert "MISMATCH" in out.getvalue() and "accepted_restores" in out.getvalue()
