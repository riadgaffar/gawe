"""Metrics checked against a small example worked out by hand.

Four contracts, three scored kinds (ul = unlimited_liability, nc = non_compete, ip = ip_assignment):

    contract   CUAD says   repeat 1 flags   repeat 2 flags
    A          ul          ul               ul
    B          nc          ul, nc           nc
    C          ip          (nothing)        ip
    D          (nothing)   ip               (nothing)

Repeat 1:  ul tp1 fp1 | nc tp1 | ip fp1 fn1  ->  micro tp2 fp2 fn1 tn7: P 1/2, R 2/3, F1 4/7
Repeat 2:  everything right                  ->  micro tp3: P 1, R 1, F1 1
Flips: B-ul, C-ip, D-ip change between repeats, out of 12 pairs -> 1/4
"""

from typing import Any

import pytest

from bench.cuad.labels import CuadCase
from bench.metrics import (
    Counts,
    averaged_predictions,
    bootstrap_interval,
    confusion,
    failure_rate,
    flip_rate,
    micro,
    per_repeat,
    prevalence,
    scored_trials,
    spread,
    summarize_cuad,
)

UL, NC, IP = "unlimited_liability", "non_compete", "ip_assignment"
KINDS = (UL, NC, IP)


def case(case_id: str, *present: str) -> CuadCase:
    return CuadCase(
        case_id=case_id,
        contract_type="T",
        text_path="",
        text_sha256="",
        chars=0,
        labels={k: k in present for k in KINDS},
        partial_labels={"unilateral_termination": False, "auto_renewal": "auto_renewal" in present},
    )


CASES = {
    "A": case("A", UL),
    "B": case("B", NC),
    "C": case("C", IP),
    "D": case("D", "auto_renewal"),
}


def trial(case_id: str, repeat: int, *flagged: str, **kw: Any) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "repeat": repeat,
        "profile": kw.get("profile", "presence"),
        "status": kw.get("status", "ok"),
        "flagged": sorted(flagged),
    }


REPEAT_1 = [trial("A", 1, UL), trial("B", 1, UL, NC), trial("C", 1), trial("D", 1, IP)]
REPEAT_2 = [trial("A", 2, UL), trial("B", 2, NC), trial("C", 2, IP), trial("D", 2)]
TRIALS = REPEAT_1 + REPEAT_2


def test_confusion_per_kind_matches_the_hand_count() -> None:
    assert confusion(REPEAT_1, CASES, UL) == Counts(tp=1, fp=1, fn=0, tn=2)
    assert confusion(REPEAT_1, CASES, NC) == Counts(tp=1, fp=0, fn=0, tn=3)
    assert confusion(REPEAT_1, CASES, IP) == Counts(tp=0, fp=1, fn=1, tn=2)


def test_precision_recall_and_f1() -> None:
    total = micro({k: confusion(REPEAT_1, CASES, k) for k in KINDS})
    assert total == Counts(tp=2, fp=2, fn=1, tn=7)
    assert total.precision == pytest.approx(1 / 2)
    assert total.recall == pytest.approx(2 / 3)
    assert total.f1 == pytest.approx(4 / 7)


def test_undefined_metrics_are_none_not_zero_or_one() -> None:
    assert Counts().precision is None and Counts().recall is None and Counts().f1 is None
    ip = confusion(REPEAT_1, CASES, IP)
    assert ip.precision == 0 and ip.recall == 0 and ip.f1 is None


def test_partial_kinds_are_scored_from_their_own_labels() -> None:
    renewal = confusion([trial("D", 1, "auto_renewal"), trial("A", 1)], CASES, "auto_renewal")
    assert renewal == Counts(tp=1, tn=1)


def test_spread_across_repeats() -> None:
    precision = per_repeat(TRIALS, CASES, KINDS, "precision")
    assert precision.values == pytest.approx((0.5, 1.0))
    assert (precision.mean, precision.low, precision.high) == pytest.approx((0.75, 0.5, 1.0))
    assert spread([None, None]).mean is None


def test_flip_rate_matches_the_hand_count() -> None:
    assert flip_rate(TRIALS, KINDS) == pytest.approx(3 / 12)
    assert flip_rate(REPEAT_1, KINDS) is None  # nothing was read twice


def test_predictions_are_averaged_across_repeats() -> None:
    averaged = averaged_predictions(TRIALS, KINDS)
    assert averaged["B"] == {UL: 0.5, NC: 1.0, IP: 0.0}
    assert averaged["A"][UL] == 1.0


def test_failed_reads_count_as_flagging_nothing_and_are_reported() -> None:
    trials = [*REPEAT_2[:3], trial("D", 2, status="reader_error")]
    assert micro({k: confusion(scored_trials(trials, "presence"), CASES, k) for k in KINDS}) == (
        Counts(tp=3, fp=0, fn=0, tn=9)
    )
    assert failure_rate(trials) == pytest.approx(1 / 4)
    missed = [*REPEAT_2[:2], trial("C", 2, status="unavailable"), REPEAT_2[3]]
    assert confusion(scored_trials(missed, "presence"), CASES, IP).fn == 1


def test_budget_stopped_and_other_profiles_are_left_out() -> None:
    trials = [
        trial("A", 1, UL),
        trial("B", 1, status="budget_stopped"),
        trial("C", 1, IP, profile="production"),
    ]
    assert [t["case_id"] for t in scored_trials(trials, "presence")] == ["A"]
    assert failure_rate([]) is None


def test_bootstrap_is_tight_when_every_read_is_right() -> None:
    assert bootstrap_interval(REPEAT_2, CASES, KINDS, "recall", seed=1) == (1.0, 1.0)


def test_bootstrap_brackets_the_estimate_and_is_reproducible() -> None:
    first = bootstrap_interval(TRIALS, CASES, KINDS, "precision", resamples=500, seed=3)
    assert first == bootstrap_interval(TRIALS, CASES, KINDS, "precision", resamples=500, seed=3)
    assert first is not None and first[0] <= 0.75 <= first[1]


def test_bootstrap_without_data_is_none() -> None:
    assert bootstrap_interval([], CASES, KINDS, "f1") is None
    # Every resample has no positive predictions, so precision is undefined throughout.
    assert bootstrap_interval([trial("A", 1)], CASES, KINDS, "precision", resamples=20) is None


def test_prevalence() -> None:
    assert prevalence(CASES.values(), UL) == pytest.approx(1 / 4)
    assert prevalence([], UL) is None


def test_summary_labels_production_recall_as_a_lower_bound() -> None:
    production = [{**t, "profile": "production"} for t in TRIALS]
    summary = summarize_cuad(production, CASES, profile="production", kinds=KINDS, resamples=50)
    assert summary["recall_is_lower_bound"] is True
    presence = summarize_cuad(TRIALS, CASES, profile="presence", kinds=KINDS, resamples=50)
    assert presence["recall_is_lower_bound"] is False
    assert presence["reads"] == 8 and presence["contracts"] == 4
    assert presence["micro"]["tp"] == 5 and presence["flip_rate"] == pytest.approx(0.25)
    assert presence["micro"]["per_repeat"]["precision"]["low"] == pytest.approx(0.5)
    assert presence["kinds"][UL]["fp"] == 1
