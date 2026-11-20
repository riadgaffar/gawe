"""The D1 check (M17): `unlimited_liability` flags split by what CUAD says about the cap.

CUAD marks a contract "Uncapped Liability" (some liability uncapped, including carve-outs), or
"Cap on Liability" only, or neither (no cap clause). M12 showed most false flags fall in the last
two groups, so the bars in docs/m17-plan.md are set per group. This module scores a run against
them, and optionally shows an earlier run (M12) on the same contracts next to it.

Presence is scored on every flag. Production is scored on high-severity flags only, since that is
what the contradiction rule reads; a run from before M17 kept no severity, so its production
reads are scored at any severity and say so.

The bars are the plan's, fixed before the paid run, and are only reported here.
"""

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from bench.cuad.labels import FILENAME_COLUMN
from bench.metrics import wilson
from bench.results import ResultsWriter

KIND = "unlimited_liability"
CAP_COLUMN = "Cap On Liability-Answer"
UNCAPPED_COLUMN = "Uncapped Liability-Answer"
STEADY_READS = 2  # a contract "flagged" when at least this many of its reads flag it

Group = Literal["uncapped", "capped", "no_cap"]
GROUPS: tuple[Group, ...] = ("uncapped", "capped", "no_cap")

# docs/m17-plan.md, "Bars, fixed before any paid run".
M12_PRECISION = {"presence": 30 / 91, "production": 13 / 66}
PRESENCE_BARS = {
    "precision_min": 0.60,
    "uncapped_contracts_min": 9,
    "uncapped_reads_min": 27,
    "other_contracts_max": 10,
}
PRODUCTION_BARS = {"precision_min": 0.50, "uncapped_reads_min": 13, "no_cap_high_share_max": 0.10}


def cap_groups(root: Path, csv_name: str = "master_clauses.csv") -> dict[str, Group]:
    """case_id -> what CUAD says about the contract's liability cap."""
    groups: dict[str, Group] = {}
    with (root / csv_name).open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            case_id = f"cuad:{Path(row[FILENAME_COLUMN].strip()).stem.strip()}"
            uncapped = row[UNCAPPED_COLUMN].strip().lower() == "yes"
            capped = row[CAP_COLUMN].strip().lower() == "yes"
            groups[case_id] = "uncapped" if uncapped else ("capped" if capped else "no_cap")
    return groups


def _flagged(trial: Mapping[str, Any], high_only: bool) -> bool:
    if high_only:
        return any(f["kind"] == KIND and f["severity"] == "high" for f in trial["flags"])
    return KIND in trial["flagged"]


def _share(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "rate": k / n if n else None, "wilson_95": wilson(k, n)}


def summarize_profile(
    trials: Sequence[Mapping[str, Any]], groups: Mapping[str, Group], profile: str
) -> dict[str, Any]:
    reads = [
        t
        for t in trials
        if t.get("kind", "cuad") == "cuad" and t["profile"] == profile and t["status"] == "ok"
    ]
    high_only = profile == "production" and bool(reads) and all("flags" in t for t in reads)
    per_group: dict[str, dict[str, Any]] = {}
    counts: dict[str, dict[str, int]] = {}
    for group in GROUPS:
        mine = [t for t in reads if groups.get(t["case_id"]) == group]
        by_contract: dict[str, int] = {}
        for t in mine:
            by_contract[t["case_id"]] = by_contract.get(t["case_id"], 0) + _flagged(t, high_only)
        per_group[group] = {
            "reads": len(mine),
            "flagged_reads": sum(by_contract.values()),
            "contracts": len(by_contract),
            "steady_contracts": sum(1 for n in by_contract.values() if n >= STEADY_READS),
        }
        counts[group] = by_contract
        if profile == "production" and high_only:
            per_group[group]["medium_reads"] = sum(
                1
                for t in mine
                if any(f["kind"] == KIND and f["severity"] == "medium" for f in t["flags"])
            )
    true_flags = per_group["uncapped"]["flagged_reads"]
    all_flags = sum(g["flagged_reads"] for g in per_group.values())
    return {
        "profile": profile,
        "scored_on": "high severity" if high_only else "any severity",
        "reads": len(reads),
        "missing_group": sum(1 for t in reads if t["case_id"] not in groups),
        "groups": per_group,
        "precision": _share(true_flags, all_flags),
        "per_contract": {g: dict(sorted(c.items())) for g, c in counts.items()},
    }


def _bar(value: float | int | None, limit: float, kind: str) -> dict[str, Any]:
    ok = value is not None and (value >= limit if kind == "min" else value <= limit)
    return {"value": value, "limit": limit, "kind": kind, "passes": ok}


def check_bars(summary: Mapping[str, Any]) -> dict[str, Any]:
    profile, g, precision = summary["profile"], summary["groups"], summary["precision"]
    lower = precision["wilson_95"][0] if precision["wilson_95"] else None
    m12 = M12_PRECISION[profile]
    bars: dict[str, Any] = {}
    if profile == "presence":
        b = PRESENCE_BARS
        bars["precision"] = _bar(precision["rate"], b["precision_min"], "min")
        bars["uncapped_contracts"] = _bar(
            g["uncapped"]["steady_contracts"], b["uncapped_contracts_min"], "min"
        )
        bars["uncapped_reads"] = _bar(
            g["uncapped"]["flagged_reads"], b["uncapped_reads_min"], "min"
        )
        other = g["capped"]["steady_contracts"] + g["no_cap"]["steady_contracts"]
        bars["other_contracts"] = _bar(other, b["other_contracts_max"], "max")
    else:
        b = PRODUCTION_BARS
        bars["precision"] = _bar(precision["rate"], b["precision_min"], "min")
        bars["uncapped_reads"] = _bar(
            g["uncapped"]["flagged_reads"], b["uncapped_reads_min"], "min"
        )
        no_cap = g["no_cap"]
        share = no_cap["flagged_reads"] / no_cap["reads"] if no_cap["reads"] else None
        bars["no_cap_high_share"] = _bar(share, b["no_cap_high_share_max"], "max")
    # Strictly above M12's precision, not equal: "clearly higher" in the plan.
    bars["precision_lower_above_m12"] = {
        "value": lower,
        "limit": m12,
        "kind": "min",
        "passes": lower is not None and lower > m12,
    }
    if summary["scored_on"] != "high severity" and profile == "production":
        for bar in bars.values():
            bar["passes"] = False  # production's bars are on high-severity flags only
    return {"bars": bars, "passes": all(bar["passes"] for bar in bars.values())}


def build_liability(
    directory: Path, groups: Mapping[str, Group], baseline: Path | None = None
) -> dict[str, Any]:
    trials = ResultsWriter(directory).trials("cuad")
    profiles = sorted({t["profile"] for t in trials})
    out: dict[str, Any] = {"run": str(directory), "profiles": {}}
    for profile in profiles:
        summary = summarize_profile(trials, groups, profile)
        out["profiles"][profile] = {**summary, **check_bars(summary)}
    if baseline is not None:
        old = ResultsWriter(baseline).trials("cuad")
        out["baseline"] = {
            "run": str(baseline),
            "profiles": {p: summarize_profile(old, groups, p) for p in profiles},
        }
    return out


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _value(bar: Mapping[str, Any]) -> str:
    value = bar["value"]
    if isinstance(value, float):
        return _pct(value)
    return "n/a" if value is None else str(value)


def _limit(name: str, bar: Mapping[str, Any]) -> str:
    sign = ">=" if bar["kind"] == "min" else "<="
    if name == "precision_lower_above_m12":
        return f"> {_pct(bar['limit'])}"
    limit = bar["limit"]
    return f"{sign} {_pct(limit) if isinstance(limit, float) and limit < 1 else limit}"


def render_liability(report: Mapping[str, Any]) -> str:
    lines = ["# D1 check: `unlimited_liability` by CUAD cap group", ""]
    for profile, s in report["profiles"].items():
        lines += [f"## `{profile}` (scored on {s['scored_on']} flags)", ""]
        lines += [
            "| CUAD says | Flagged reads | Contracts in 2+ reads | Medium reads |",
            "|---|---|---|---|",
        ]
        base = report.get("baseline", {}).get("profiles", {}).get(profile)
        for group in GROUPS:
            g = s["groups"][group]
            medium = str(g.get("medium_reads", "n/a"))
            row = (
                f"| {group} | {g['flagged_reads']} of {g['reads']} | "
                f"{g['steady_contracts']} of {g['contracts']} | {medium} |"
            )
            lines.append(row)
        p = s["precision"]
        span = p["wilson_95"]
        lines += [
            "",
            f"Precision {p['k']} of {p['n']} ({_pct(p['rate'])}, 95% "
            f"{_pct(span[0]) if span else 'n/a'}–{_pct(span[1]) if span else 'n/a'}).",
            "",
        ]
        if base is not None:
            bp = base["precision"]
            lines += [
                f"Baseline ({base['scored_on']}): precision {bp['k']} of {bp['n']} "
                f"({_pct(bp['rate'])}); "
                + "; ".join(
                    f"{grp} {base['groups'][grp]['flagged_reads']} of "
                    f"{base['groups'][grp]['reads']} reads, "
                    f"{base['groups'][grp]['steady_contracts']} contracts"
                    for grp in GROUPS
                )
                + ".",
                "",
            ]
        lines += ["| Bar (plan) | Value | Limit | Passes |", "|---|---|---|---|"]
        for name, bar in s["bars"].items():
            lines.append(
                f"| {name} | {_value(bar)} | {_limit(name, bar)} | "
                f"{'yes' if bar['passes'] else 'no'} |"
            )
        lines += ["", f"All bars pass: {'yes' if s['passes'] else 'no'}.", ""]
    return "\n".join(lines)


def write_liability(
    directory: Path, groups: Mapping[str, Group], baseline: Path | None = None
) -> tuple[Path, Path]:
    report = build_liability(directory, groups, baseline)
    json_path, md_path = directory / "liability.json", directory / "liability.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_liability(report), encoding="utf-8")
    return json_path, md_path
