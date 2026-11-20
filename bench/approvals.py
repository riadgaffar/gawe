"""How many honest contracts each approval rule would send to a person (M17, design D3).

Reads a run's `production` CUAD records, which since M17 keep each flag's severity and the risk
level, and works out per read what contract review's rules would do with a "low" rating:

- `current`: today's contradiction rule, a "low" with a high-severity flag;
- `current_plus_scan`: the same, or a "low" on a document the express liability scan hit;
- `every_low`: every "low" needs a person (approval by change, applied to "low" ratings only);
- `carve_out_queue`: a "low" with a carve-out hit and no express hit, which goes to next-day
  review rather than to legal.

The scan rows need a scan of each contract (S4); without one they are left out. The deal-size
rule cannot be worked out here: CUAD has no deal values, so it adds an unknown share on top.

The bars are the plan's (docs/m17-plan.md), fixed before the paid runs. They are reported, never
tuned here. Nothing is called and nothing is spent.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.metrics import wilson
from bench.results import ResultsWriter

SCHEMA = "gawe.bench.approvals.v1"
PROFILE = "production"

# docs/m17-plan.md, "Bars, fixed before any paid run". Upper ends of 95% Wilson intervals.
D4_EVERY_LOW_MAX = 0.20
D2_EXPRESS_ADDED_MAX = 0.05
D2_CARVE_OUT_QUEUE_MAX = 0.15


@dataclass(frozen=True)
class ScanHit:
    express: bool
    carve_out: bool


Scan = Callable[[str], ScanHit]  # case_id -> what the liability scan found in that contract


def _share(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "rate": k / n if n else None, "wilson_95": wilson(k, n)}


def _upper(share: Mapping[str, Any]) -> float | None:
    interval = share["wilson_95"]
    return None if interval is None else interval[1]


def _bar(share: Mapping[str, Any], limit: float) -> dict[str, Any]:
    upper = _upper(share)
    return {"limit": limit, "upper_95": upper, "passes": upper is not None and upper <= limit}


def summarize_approvals(
    trials: Sequence[Mapping[str, Any]], scan: Scan | None = None, *, express_rule: bool = True
) -> dict[str, Any]:
    """Approval outcomes over the profile's successful reads that recorded severities.

    `express_rule` is False when the express level failed its free gate: then no scan rule sends
    anything to legal, and every scan hit, express or carve-out, goes to next-day review, so the
    review-queue bar counts both levels (docs/m17-plan.md, "D2, express level").
    """
    mine = [t for t in trials if t.get("kind", "cuad") == "cuad" and t["profile"] == PROFILE]
    ok = [t for t in mine if t["status"] == "ok"]
    recorded = [t for t in ok if "flags" in t and "risk_level" in t]
    n = len(recorded)

    def low(t: Mapping[str, Any]) -> bool:
        return bool(t["risk_level"] == "low")

    def high(t: Mapping[str, Any]) -> bool:
        return any(f["severity"] == "high" for f in t["flags"])

    levels: dict[str, int] = {}
    for t in recorded:
        key = str(t["risk_level"])
        levels[key] = levels.get(key, 0) + 1

    outcomes: dict[str, Any] = {
        "rated_low": _share(sum(1 for t in recorded if low(t)), n),
        "current": _share(sum(1 for t in recorded if low(t) and high(t)), n),
        "every_low": _share(sum(1 for t in recorded if low(t)), n),
    }
    bars: dict[str, Any] = {"d4_every_low": _bar(outcomes["every_low"], D4_EVERY_LOW_MAX)}
    if scan is not None and not express_rule:
        hits = {t["case_id"]: scan(t["case_id"]) for t in recorded}
        outcomes["review_queue"] = _share(
            sum(
                1
                for t in recorded
                if low(t) and (hits[t["case_id"]].express or hits[t["case_id"]].carve_out)
            ),
            n,
        )
        bars["d2_review_queue"] = _bar(outcomes["review_queue"], D2_CARVE_OUT_QUEUE_MAX)
    elif scan is not None:
        hits = {t["case_id"]: scan(t["case_id"]) for t in recorded}
        express_added = sum(
            1 for t in recorded if low(t) and hits[t["case_id"]].express and not high(t)
        )
        outcomes["current_plus_scan"] = _share(
            sum(1 for t in recorded if low(t) and (high(t) or hits[t["case_id"]].express)), n
        )
        outcomes["express_added"] = _share(express_added, n)
        outcomes["carve_out_queue"] = _share(
            sum(
                1
                for t in recorded
                if low(t) and hits[t["case_id"]].carve_out and not hits[t["case_id"]].express
            ),
            n,
        )
        bars["d2_express_added"] = _bar(outcomes["express_added"], D2_EXPRESS_ADDED_MAX)
        bars["d2_carve_out_queue"] = _bar(outcomes["carve_out_queue"], D2_CARVE_OUT_QUEUE_MAX)

    return {
        "schema": SCHEMA,
        "profile": PROFILE,
        "reads": n,
        "contracts": len({t["case_id"] for t in recorded}),
        "failed_reads": len(mine) - len(ok),
        "unrecorded_reads": len(ok) - n,  # records from before M17 keep no severity
        "risk_levels": dict(sorted(levels.items())),
        "scanned": scan is not None,
        "express_rule": express_rule,
        "outcomes": outcomes,
        "bars": bars,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _row(name: str, share: Mapping[str, Any]) -> str:
    interval = share["wilson_95"]
    span = "n/a" if interval is None else f"{_pct(interval[0])}–{_pct(interval[1])}"
    return f"| {name} | {share['k']} of {share['n']} | {_pct(share['rate'])} | {span} |"


_NAMES = {
    "rated_low": 'Rated "low"',
    "current": "Current rules (low with a high-severity flag)",
    "current_plus_scan": "Current rules plus the express scan rule",
    "express_added": "Added by the express scan rule",
    "every_low": 'Every "low" needs a person',
    "carve_out_queue": "Carve-out only, queued for next-day review",
    "review_queue": "Any scan hit, queued for next-day review (no express rule)",
}


def render_approvals(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Approval volume on honest contracts",
        "",
        f"Production reads: {summary['reads']} on {summary['contracts']} contracts "
        f"({summary['failed_reads']} failed reads left out, {summary['unrecorded_reads']} "
        "without recorded severities). Rates are per read, with 95% Wilson intervals.",
        "",
        "| Outcome | Count | Share | 95% interval |",
        "|---|---|---|---|",
    ]
    lines += [_row(_NAMES[k], v) for k, v in summary["outcomes"].items()]
    lines += [
        "",
        "Risk levels: " + ", ".join(f"{k} {v}" for k, v in summary["risk_levels"].items()),
    ]
    if not summary["scanned"]:
        lines += ["", "No liability scan was given, so the scan rows are left out."]
    lines += ["", "| Bar (plan) | Upper 95% | Limit | Passes |", "|---|---|---|---|"]
    for name, bar in summary["bars"].items():
        lines.append(
            f"| {name} | {_pct(bar['upper_95'])} | {_pct(bar['limit'])} | "
            f"{'yes' if bar['passes'] else 'no'} |"
        )
    lines += [
        "",
        "The deal-size rule is not counted: CUAD has no deal values, so it adds an unknown share "
        "on top. CUAD contracts are not Acme's, so this is a traffic stand-in, not a forecast.",
        "",
    ]
    return "\n".join(lines)


def write_approvals(
    directory: Path, scan: Scan | None = None, *, express_rule: bool = True
) -> tuple[Path, Path]:
    summary = summarize_approvals(
        ResultsWriter(directory).trials("cuad"), scan, express_rule=express_rule
    )
    json_path, md_path = directory / "approvals.json", directory / "approvals.md"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_approvals(summary), encoding="utf-8")
    return json_path, md_path
