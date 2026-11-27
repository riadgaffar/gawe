"""M29 S3: the packaged demo's scenes, played headless (`python -m bench demo`).

Each case's expected record is written by hand in bench/demo.py. These tests run them, check the
design's table (docs/m29-design.md, D4) directly against what runs, and check that the command
writes every record and reports a changed outcome.
"""

import io
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bench.cli import main
from bench.demo import CASES, differences, expected_record, run_case, run_suite
from bench.harness import Deps

BY_ID = {c.case_id: c for c in CASES}


def outcome(company: str, scene: str, mode: str, step: str) -> Any:
    record = run_case(BY_ID[f"{company}:{scene}:{mode}"])
    return next(s[1] for s in record["steps"] if s[0] == step), record["status"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_each_case_matches_its_expected_record(case: Any) -> None:
    assert differences(run_case(case), expected_record(case)) == {}


def test_every_scene_runs_at_every_company() -> None:
    from gawe.demo.companies import COMPANIES
    from gawe.demo.scenes import SCENES

    expected = {
        f"{c.key}:{s.scene_id}:{m}"
        for c in COMPANIES
        for s in SCENES
        for m in (("honest", "fooled") if s.fooled else ("honest",))
    }
    assert set(BY_ID) == expected


# The design's table, D4, row by row.
@pytest.mark.parametrize(
    ("company", "decided"),
    [("careful", "needs_approval"), ("big-deals", "allow"), ("rivals", "needs_approval")],
)
def test_an_honest_low_on_a_small_deal(company: str, decided: str) -> None:
    assert outcome(company, "c1001-review", "honest", "assess")[0] == decided


@pytest.mark.parametrize(
    ("company", "decided"),
    [("careful", "needs_approval"), ("big-deals", "allow"), ("rivals", "needs_approval")],
)
def test_a_fooled_low_goes_through_only_at_big_deals(company: str, decided: str) -> None:
    assert outcome(company, "c1003-review", "fooled", "assess")[0] == decided


@pytest.mark.parametrize("company", ["careful", "big-deals", "rivals"])
def test_emailing_the_attacker_is_denied_everywhere(company: str) -> None:
    assert outcome(company, "c1003-email-counsel", "fooled", "email") == ("deny", "denied")


@pytest.mark.parametrize(
    ("company", "decided"),
    [("careful", "needs_approval"), ("big-deals", "needs_approval"), ("rivals", "deny")],
)
def test_a_competitors_domain_is_blocked_only_at_rivals(company: str, decided: str) -> None:
    assert outcome(company, "c1001-email-competitor", "honest", "email")[0] == decided


@pytest.mark.parametrize("company", ["careful", "big-deals", "rivals"])
def test_a_large_signature_needs_finance_everywhere(company: str) -> None:
    record = run_case(BY_ID[f"{company}:c1002-signature:honest"])
    assert record["steps"][-1] == ["sign", "needs_approval", "finance_approver"]


def test_demo_command_writes_every_record(tmp_path: Path) -> None:
    out = io.StringIO()
    deps = Deps(
        results_dir=tmp_path,
        now=lambda: datetime(2026, 11, 26, 12, tzinfo=UTC),
        git=lambda: ("abc123", True),
    )
    assert main(["demo"], out=out, deps=deps) == 0
    directory = tmp_path / "20261126T120000Z-demo"
    trials = [json.loads(line) for line in (directory / "trials.jsonl").read_text().splitlines()]
    assert [t["case"] for t in trials] == [c.case_id for c in CASES]
    assert all(t["matches_expected"] and t["kind"] == "demo_case" for t in trials)
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["kind"] == "demo" and manifest["model"] is None
    assert f"{len(CASES)}/{len(CASES)} match their expected records" in out.getvalue()


def test_a_changed_outcome_is_reported(tmp_path: Path) -> None:
    case = BY_ID["big-deals:c1001-review:honest"]
    wrong = replace(case, expected={**case.expected, "status": "paused_for_approval"})
    out = io.StringIO()
    assert not run_suite(tmp_path, stamp="x", git=("abc", False), out=out, cases=[wrong])
    assert "MISMATCH" in out.getvalue() and "status" in out.getvalue()
