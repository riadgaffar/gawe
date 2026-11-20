"""M17 D1: unlimited_liability scored by CUAD cap group against the plan's bars."""

import csv
from pathlib import Path
from typing import Any

from bench.liability import cap_groups, check_bars, summarize_profile


def read(case: str, profile: str, *severities: str, repeat: int = 1, **extra: Any) -> dict:
    flags = [{"kind": "unlimited_liability", "severity": s} for s in severities]
    return {
        "kind": "cuad",
        "case_id": case,
        "profile": profile,
        "repeat": repeat,
        "status": "ok",
        "flagged": ["unlimited_liability"] if flags else [],
        "flags": flags,
        **extra,
    }


GROUPS = {"cuad:u": "uncapped", "cuad:c": "capped", "cuad:n": "no_cap"}


def test_cap_groups_reads_both_cuad_columns(tmp_path: Path) -> None:
    rows = [("u.pdf", "Yes", "Yes"), ("c.pdf", "Yes", "No"), ("n.pdf", "No", "No")]
    with (tmp_path / "master_clauses.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Filename", "Cap On Liability-Answer", "Uncapped Liability-Answer"])
        writer.writerows(rows)
    assert cap_groups(tmp_path) == GROUPS


def test_production_counts_only_high_severity_and_reports_medium() -> None:
    trials = [read("cuad:u", "production", "high", repeat=r) for r in (1, 2, 3)] + [
        read("cuad:n", "production", "medium", repeat=1),
        read("cuad:n", "production", "medium", repeat=2),
        read("cuad:n", "production", "high", repeat=3),
        read("cuad:c", "production", repeat=1),
    ]
    s = summarize_profile(trials, GROUPS, "production")
    assert s["scored_on"] == "high severity"
    assert s["groups"]["no_cap"] == {
        "reads": 3,
        "flagged_reads": 1,
        "contracts": 1,
        "steady_contracts": 0,
        "medium_reads": 2,
    }
    assert (s["precision"]["k"], s["precision"]["n"]) == (3, 4)


def test_old_production_records_are_scored_at_any_severity_and_never_pass() -> None:
    old = [{k: v for k, v in read("cuad:u", "production", "high").items() if k != "flags"}]
    s = summarize_profile(old, GROUPS, "production")
    assert s["scored_on"] == "any severity"
    assert check_bars(s)["passes"] is False


def test_presence_bars_follow_the_plan() -> None:
    # 10 uncapped contracts flagged in all 3 reads; 2 capped contracts flagged once each.
    trials = [
        read(f"cuad:u{i}", "presence", "high", repeat=r) for i in range(10) for r in (1, 2, 3)
    ]
    trials += [read(f"cuad:c{i}", "presence", "high") for i in range(2)]
    groups = {
        **{f"cuad:u{i}": "uncapped" for i in range(10)},
        "cuad:c0": "capped",
        "cuad:c1": "capped",
    }
    result = check_bars(summarize_profile(trials, groups, "presence"))
    bars = result["bars"]
    assert bars["uncapped_contracts"]["value"] == 10 and bars["other_contracts"]["value"] == 0
    assert bars["precision"]["value"] == 30 / 32
    assert result["passes"] is True


def test_the_lower_bound_must_be_strictly_above_m12() -> None:
    trials = [read("cuad:u", "presence", "high"), read("cuad:n", "presence", "high")]
    bars = check_bars(summarize_profile(trials, GROUPS, "presence"))["bars"]
    assert bars["precision_lower_above_m12"]["passes"] is False  # n=2: interval far too wide


def test_failed_reads_and_unknown_contracts_are_left_out() -> None:
    trials = [
        read("cuad:u", "presence", "high"),
        {**read("cuad:u", "presence"), "status": "unavailable"},
    ]
    trials.append(read("cuad:x", "presence", "high"))
    s = summarize_profile(trials, GROUPS, "presence")
    assert s["reads"] == 2 and s["missing_group"] == 1
    assert s["groups"]["uncapped"]["reads"] == 1
