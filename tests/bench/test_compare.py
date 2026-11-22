"""M18 D5: the paired comparison of two models on the same contracts and attacks."""

from typing import Any

from bench.compare import paired_cuad, suite
from bench.cuad.labels import CuadCase

KINDS = ["unlimited_liability", "non_compete"]


def contract(i: int, liability: bool, compete: bool) -> CuadCase:
    return CuadCase(
        case_id=f"cuad:c{i}",
        contract_type="Service",
        text_path="x.txt",
        text_sha256="0" * 64,
        chars=10,
        labels={"unlimited_liability": liability, "non_compete": compete, "ip_assignment": False},
        partial_labels={},
    )


CASES = {c.case_id: c for c in (contract(i, i % 2 == 0, i % 3 == 0) for i in range(20))}


def reads(predict, profile: str = "presence", repeats: int = 3) -> list[dict[str, Any]]:
    out = []
    for case in CASES.values():
        for r in range(1, repeats + 1):
            flagged = [k for k in KINDS if predict(case, k, r)]
            out.append(
                {
                    "kind": "cuad",
                    "case_id": case.case_id,
                    "profile": profile,
                    "repeat": r,
                    "status": "ok",
                    "flagged": flagged,
                }
            )
    return out


def truth(case: CuadCase, kind: str, repeat: int) -> bool:
    return case.labels[kind]


def always_liability(case: CuadCase, kind: str, repeat: int) -> bool:
    return kind == "unlimited_liability" or case.labels[kind]  # over-flags liability


def run(a, b) -> dict[str, Any]:
    return paired_cuad(a, b, CASES, "presence", kinds=KINDS, resamples=500)


def test_a_model_against_itself_shows_no_difference() -> None:
    same = reads(truth)
    result = run(same, same)
    assert result["disagreements"] == {"by_majority": 0, "steady": 0, "floor_p": None}
    assert result["accuracy"]["mean_score_difference"] == 0
    assert all(r["difference"] == 0 for r in result["rates"].values())
    assert result["accuracy"]["p_value"] == 1.0


def test_a_better_model_shows_a_clear_paired_difference() -> None:
    result = run(reads(truth), reads(always_liability))
    precision = result["rates"]["precision"]
    assert precision["difference"] > 0.2 and precision["interval_95"][0] > 0
    assert precision["p_value"] < 0.01
    assert result["accuracy"]["mean_score_difference"] > 0 and result["accuracy"]["p_value"] < 0.01
    # The ten contracts without uncapped liability are where the two disagree, every repeat.
    assert result["disagreements"]["by_majority"] == 10 == result["disagreements"]["steady"]


def test_noise_that_flips_one_repeat_is_a_disagreement_only_by_majority_when_it_wins() -> None:
    def noisy(case: CuadCase, kind: str, repeat: int) -> bool:
        flip = case.case_id == "cuad:c1" and kind == "non_compete" and repeat == 1
        return case.labels[kind] != flip

    result = run(reads(noisy), reads(truth))
    assert result["disagreements"]["by_majority"] == 0  # 1 of 3 reads is not a majority
    assert result["disagreements"]["steady"] == 0


def test_production_is_scored_on_high_severity_flags() -> None:
    def with_flags(predict, severity: str) -> list[dict[str, Any]]:
        out = reads(predict, profile="production")
        for t in out:
            t["flags"] = [{"kind": k, "severity": severity} for k in t["flagged"]]
        return out

    # Flags at medium don't count, so A (all medium) finds nothing that B (all high) finds.
    result = paired_cuad(
        with_flags(truth, "medium"),
        with_flags(truth, "high"),
        CASES,
        "production",
        kinds=KINDS,
        resamples=200,
    )
    assert result["scored_on"] == "high severity"
    assert result["rates"]["recall"]["a"] == 0 and result["rates"]["recall"]["b"] == 1


def test_the_suite_side_counts_fooled_runs_and_steps_that_ran() -> None:
    records = [
        {
            "kind": "injection",
            "twin": False,
            "case_id": "a",
            "model_fooled": True,
            "reader_fooled": True,
        },
        {
            "kind": "injection",
            "twin": False,
            "case_id": "b",
            "model_fooled": True,
            "planner_fooled": True,
            "unauthorized_step_ran": True,
        },
        {
            "kind": "injection",
            "twin": False,
            "case_id": "c",
            "model_fooled": None,
            "status": "reader_error",
            "reader_failures": ["summary:maxLength"],
        },
        {"kind": "injection", "twin": True, "case_id": "twin-x", "model_fooled": None},
    ]
    summary = suite(records)
    assert (summary["attack_runs"], summary["judged"]) == (3, 2)
    assert (summary["reader_fooled"], summary["planner_fooled"], summary["ran"]) == (1, 1, ["b"])
    assert summary["reader_errors"]["labels"] == {"summary:maxLength": 1}
