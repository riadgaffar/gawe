"""The D2 gate (M17): the liability scan over every eligible CUAD contract, before any rule uses it.

Free: no model is called. Each contract is scanned and compared with CUAD's "Uncapped Liability"
label. The express level must clear the plan's two bars (docs/m17-plan.md, "D2, express level")
to feed a rule; the third bar, how much it adds to approvals, needs the D3 reads and is in
`python -m bench approvals`. The carve-out level has no bar here; its rate is reported.

The scan's patterns were frozen before this ran. Nothing here tunes them.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bench.cuad.labels import CuadCase, read_text
from bench.metrics import wilson
from gawe.apps.contract_review.liability_scan import CARVE_OUT, EXPRESS, VERSION, LiabilityScan
from gawe.apps.contract_review.liability_scan import scan as scan_text

KIND = "unlimited_liability"

# docs/m17-plan.md, "D2, express level (free, all 495 eligible CUAD contracts)".
EXPRESS_PRECISION_MIN = 0.60
EXPRESS_FALSE_SHARE_MAX = 0.10


def scan_cases(root: Path, cases: Sequence[CuadCase]) -> dict[str, LiabilityScan]:
    return {case.case_id: scan_text(read_text(root, case)) for case in cases}


def _share(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "rate": k / n if n else None, "wilson_95": wilson(k, n)}


def summarize_gate(cases: Sequence[CuadCase], scans: Mapping[str, LiabilityScan]) -> dict[str, Any]:
    positive = [c for c in cases if c.labels.get(KIND)]
    negative = [c for c in cases if not c.labels.get(KIND)]

    def hits(group: Sequence[CuadCase], level: str) -> int:
        return sum(1 for c in group if getattr(scans[c.case_id], level))

    express_true, express_false = hits(positive, "express"), hits(negative, "express")
    precision = _share(express_true, express_true + express_false)
    false_share = _share(express_false, len(negative))
    patterns = {
        name: {
            "uncapped": sum(1 for c in positive if name in scans[c.case_id].patterns),
            "not_uncapped": sum(1 for c in negative if name in scans[c.case_id].patterns),
        }
        for name in (*EXPRESS, *CARVE_OUT)
    }
    bars = {
        "express_precision": {
            "value": precision["rate"],
            "limit": EXPRESS_PRECISION_MIN,
            "kind": "min",
            "passes": precision["rate"] is not None and precision["rate"] >= EXPRESS_PRECISION_MIN,
        },
        "express_on_not_uncapped": {
            "value": false_share["rate"],
            "limit": EXPRESS_FALSE_SHARE_MAX,
            "kind": "max",
            "passes": false_share["rate"] is not None
            and false_share["rate"] <= EXPRESS_FALSE_SHARE_MAX,
        },
    }
    carve_only = sum(
        1 for c in cases if scans[c.case_id].carve_out and not scans[c.case_id].express
    )
    return {
        "scan_version": VERSION,
        "contracts": len(cases),
        "uncapped": len(positive),
        "not_uncapped": len(negative),
        "express": {
            "precision": precision,
            "recall": _share(express_true, len(positive)),
            "on_not_uncapped": false_share,
        },
        "carve_out": {
            "any": _share(hits(cases, "carve_out"), len(cases)),
            "only": _share(carve_only, len(cases)),
            "on_uncapped": _share(hits(positive, "carve_out"), len(positive)),
            "on_not_uncapped": _share(hits(negative, "carve_out"), len(negative)),
        },
        "patterns": patterns,
        "bars": bars,
        "express_passes": all(b["passes"] for b in bars.values()),
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _row(name: str, share: Mapping[str, Any]) -> str:
    span = share["wilson_95"]
    interval = "n/a" if span is None else f"{_pct(span[0])}–{_pct(span[1])}"
    return f"| {name} | {share['k']} of {share['n']} | {_pct(share['rate'])} | {interval} |"


def render_gate(summary: Mapping[str, Any]) -> str:
    e, c = summary["express"], summary["carve_out"]
    lines = [
        "# D2 gate: the liability scan on CUAD",
        "",
        f"Scan version {summary['scan_version']}, {summary['contracts']} contracts: "
        f"{summary['uncapped']} marked uncapped by CUAD, {summary['not_uncapped']} not. "
        "No model was called.",
        "",
        "| Measure | Count | Share | 95% interval |",
        "|---|---|---|---|",
        _row("Express: precision against CUAD", e["precision"]),
        _row("Express: recall against CUAD", e["recall"]),
        _row("Express: fires on contracts not uncapped", e["on_not_uncapped"]),
        _row("Carve-out: any", c["any"]),
        _row("Carve-out only (no express hit)", c["only"]),
        _row("Carve-out on uncapped", c["on_uncapped"]),
        _row("Carve-out on not uncapped", c["on_not_uncapped"]),
        "",
        "| Pattern | Uncapped | Not uncapped |",
        "|---|---|---|",
    ]
    for name, counts in summary["patterns"].items():
        lines.append(f"| `{name}` | {counts['uncapped']} | {counts['not_uncapped']} |")
    lines += ["", "| Bar (plan) | Value | Limit | Passes |", "|---|---|---|---|"]
    for name, bar in summary["bars"].items():
        sign = ">=" if bar["kind"] == "min" else "<="
        lines.append(
            f"| {name} | {_pct(bar['value'])} | {sign} {_pct(bar['limit'])} | "
            f"{'yes' if bar['passes'] else 'no'} |"
        )
    lines += [
        "",
        f"Express level passes its free bars: {'yes' if summary['express_passes'] else 'no'}. "
        "The third bar (approvals it adds) needs the D3 reads.",
        "",
    ]
    return "\n".join(lines)


def write_gate(directory: Path, summary: Mapping[str, Any]) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path, md_path = directory / "scan-gate.json", directory / "scan-gate.md"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_gate(summary), encoding="utf-8")
    return json_path, md_path
