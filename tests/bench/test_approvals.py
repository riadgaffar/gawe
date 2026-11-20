"""M17: approval volume worked out offline from recorded production reads."""

import io
import json
from pathlib import Path
from typing import Any

import pytest

from bench.approvals import ScanHit, render_approvals, summarize_approvals, write_approvals
from bench.cli import EXIT_OK, EXIT_REFUSED, main
from bench.cuad.labels import load_corpus
from bench.harness import Deps


def read(case: str, risk: str | None, *severities: str, status: str = "ok", **extra: Any) -> dict:
    flags = [{"kind": "unlimited_liability", "severity": s} for s in severities]
    return {
        "kind": "cuad",
        "case_id": case,
        "profile": "production",
        "repeat": 1,
        "status": status,
        "flagged": sorted({f["kind"] for f in flags}),
        "flags": flags,
        "risk_level": risk,
        **extra,
    }


READS = [
    read("a", "low"),  # low, nothing flagged
    read("b", "low", "high"),  # low contradicted by a high flag
    read("c", "low", "medium"),  # low with a medium flag only
    read("d", "high", "high"),
    read("e", "medium"),
    read("f", None, status="reader_error"),
]
SCAN = {
    "a": ScanHit(express=True, carve_out=False),
    "b": ScanHit(express=True, carve_out=True),
    "c": ScanHit(express=False, carve_out=True),
    "d": ScanHit(express=False, carve_out=False),
    "e": ScanHit(express=True, carve_out=False),
}


def test_each_rule_counts_only_low_ratings_it_would_send_to_a_person() -> None:
    summary = summarize_approvals(READS)
    out = summary["outcomes"]
    assert summary["reads"] == 5 and summary["failed_reads"] == 1
    assert (out["rated_low"]["k"], out["current"]["k"], out["every_low"]["k"]) == (3, 1, 3)
    assert summary["risk_levels"] == {"high": 1, "low": 3, "medium": 1}
    assert "current_plus_scan" not in out and not summary["scanned"]


def test_the_scan_rows_follow_the_two_levels() -> None:
    summary = summarize_approvals(READS, scan=SCAN.__getitem__)
    out = summary["outcomes"]
    assert out["current_plus_scan"]["k"] == 2  # a (express) and b (high flag)
    assert out["express_added"]["k"] == 1  # a only: b was already sent by its high flag
    assert out["carve_out_queue"]["k"] == 1  # c: carve-out, no express hit; e is not low
    assert set(summary["bars"]) == {"d4_every_low", "d2_express_added", "d2_carve_out_queue"}


def test_records_without_severities_are_counted_apart_never_as_no_flag() -> None:
    old = {k: v for k, v in read("x", "low", "high").items() if k not in ("flags", "risk_level")}
    summary = summarize_approvals([*READS, old])
    assert summary["reads"] == 5 and summary["unrecorded_reads"] == 1


def test_only_production_reads_count() -> None:
    presence = {**read("a", "low"), "profile": "presence"}
    assert summarize_approvals([presence])["reads"] == 0


@pytest.mark.parametrize(
    ("lows", "passes"),
    [(10, True), (20, False)],  # of 100 reads: upper bound 17.4% passes 20%, 28.6% does not
)
def test_the_d4_bar_reads_the_upper_end_of_the_interval(lows: int, passes: bool) -> None:
    reads = [read(str(i), "low" if i < lows else "medium") for i in range(100)]
    bar = summarize_approvals(reads)["bars"]["d4_every_low"]
    assert bar["limit"] == 0.20 and bar["passes"] is passes


def test_no_reads_never_passes_a_bar() -> None:
    assert summarize_approvals([])["bars"]["d4_every_low"]["passes"] is False


def test_the_markdown_names_what_it_cannot_count() -> None:
    text = render_approvals(summarize_approvals(READS))
    assert "deal-size rule is not counted" in text and "scan rows are left out" in text


def test_the_command_writes_both_files_from_records_only(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.json").write_text("{}")
    (run / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in READS))

    def no_data() -> Path:
        raise OSError("no CUAD here")

    deps = Deps(data_root=no_data)
    err = io.StringIO()
    code = main(["approvals", str(run)], out=io.StringIO(), err=err, deps=deps)
    assert code == EXIT_OK and "no liability scan" in err.getvalue()
    data = json.loads((run / "approvals.json").read_text())
    assert data["reads"] == 5 and data["scanned"] is False and (run / "approvals.md").is_file()
    assert main(["approvals", str(tmp_path / "nowhere")], deps=deps) == EXIT_REFUSED


def test_the_command_scans_the_contracts_it_read(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "cuad"
    cases = load_corpus(fixture).cases
    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.json").write_text("{}")
    reads = [read(c.case_id, "low") for c in cases]
    (run / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in reads))
    deps = Deps(data_root=lambda: fixture)
    assert main(["approvals", str(run)], out=io.StringIO(), deps=deps) == EXIT_OK
    data = json.loads((run / "approvals.json").read_text())
    assert data["scanned"] is True and "carve_out_queue" in data["outcomes"]


def test_write_approvals_takes_a_scan(tmp_path: Path) -> None:
    (tmp_path / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in READS))
    json_path, _ = write_approvals(tmp_path, scan=SCAN.__getitem__)
    assert json.loads(json_path.read_text())["scanned"] is True


def test_without_the_express_rule_every_hit_goes_to_review_and_nothing_to_legal() -> None:
    summary = summarize_approvals(READS, scan=SCAN.__getitem__, express_rule=False)
    out = summary["outcomes"]
    assert out["review_queue"]["k"] == 3  # a (express), b (both), c (carve-out); e is not low
    assert "current_plus_scan" not in out and "express_added" not in out
    assert set(summary["bars"]) == {"d4_every_low", "d2_review_queue"}
    assert summary["express_rule"] is False
