"""M17 D2: the free gate, scored against CUAD's Uncapped Liability label."""

import io
import json
from pathlib import Path

from bench.cli import EXIT_OK, main
from bench.cuad.labels import CuadCase, load_corpus
from bench.harness import Deps
from bench.scan_gate import summarize_gate
from gawe.apps.contract_review.liability_scan import LiabilityScan

FIXTURE = Path(__file__).parent / "fixtures" / "cuad"


def case(name: str, uncapped: bool) -> CuadCase:
    return CuadCase(name, "Service", "x.txt", "0" * 64, 10, {"unlimited_liability": uncapped}, {})


def hit(express: bool, carve: bool = False) -> LiabilityScan:
    return LiabilityScan(1, express, carve, ())


def test_the_bars_follow_the_plan() -> None:
    cases = [case(f"u{i}", True) for i in range(6)] + [case(f"n{i}", False) for i in range(20)]
    scans = {c.case_id: hit(c.labels["unlimited_liability"]) for c in cases}
    scans["n0"] = hit(True)  # one false express hit: precision 6/7, 1 of 20 not uncapped
    scans["n1"] = hit(False, carve=True)
    s = summarize_gate(cases, scans)
    assert s["express"]["precision"]["k"] == 6 and s["express"]["precision"]["n"] == 7
    assert s["express"]["on_not_uncapped"]["rate"] == 1 / 20
    assert s["carve_out"]["only"]["k"] == 1
    assert s["express_passes"] is True


def test_too_many_false_express_hits_fail_the_gate() -> None:
    cases = [case("u", True)] + [case(f"n{i}", False) for i in range(10)]
    scans = {c.case_id: hit(True) for c in cases}
    s = summarize_gate(cases, scans)
    assert s["bars"]["express_precision"]["passes"] is False
    assert s["bars"]["express_on_not_uncapped"]["passes"] is False
    assert s["express_passes"] is False


def test_the_command_runs_without_a_model(tmp_path: Path) -> None:
    deps = Deps(data_root=lambda: FIXTURE, results_dir=tmp_path)
    out = io.StringIO()
    assert main(["scan-gate"], out=out, deps=deps) == EXIT_OK
    (directory,) = [p for p in tmp_path.iterdir() if p.name.endswith("-scan-gate")]
    data = json.loads((directory / "scan-gate.json").read_text())
    assert data["contracts"] == len(load_corpus(FIXTURE).cases)
    assert "contract" not in json.dumps(data["patterns"]).lower()  # pattern ids, no text
