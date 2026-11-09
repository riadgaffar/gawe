import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bench.budget import Budget, CountingProvider, Price
from bench.cuad.labels import load_corpus
from bench.cuad.profile import PROFILES
from bench.cuad.run import plan_trials, run_cuad
from bench.results import ResultsWriter
from gawe.llm.fake import FakeProvider
from gawe.llm.provider import LLMRequest, LLMResponse, LLMUnavailableError

FIXTURE = Path(__file__).parent / "fixtures" / "cuad"
CORPUS = load_corpus(FIXTURE)
PRICE = Price("test-model", Decimal("2"), Decimal("10"), date(2026, 10, 29), "test")


def findings(*kinds: str) -> dict[str, Any]:
    return {
        "parties": ["A", "B"],
        "risk_level": "unknown",
        "flagged_clauses": [{"kind": k, "severity": "medium"} for k in kinds],
        "summary": "A contract.",
    }


def counting(script: list[Any], cap: str = "100") -> CountingProvider:
    return CountingProvider(FakeProvider(script), Budget(cap=Decimal(cap), price=PRICE))


def run(
    tmp_path: Path, provider: CountingProvider, repeats: int = 1, profiles: tuple[str, ...] = ()
) -> tuple[bool, list[dict[str, Any]], list[float]]:
    sleeps: list[float] = []
    writer = ResultsWriter(tmp_path / "out")
    trials = plan_trials(CORPUS.cases, profiles or ("presence",), repeats)
    done = run_cuad(
        FIXTURE, trials, provider=provider, profiles=PROFILES, writer=writer, sleep=sleeps.append
    )
    return done, writer.trials("cuad"), sleeps


def test_presence_runs_first_and_every_repeat_before_production() -> None:
    trials = plan_trials(CORPUS.cases, ("production", "presence"), repeats=2)
    order = [(t.profile, t.repeat) for t in trials]
    assert (
        order
        == [("presence", 1)] * 3
        + [("presence", 2)] * 3
        + [("production", 1)] * 3
        + [("production", 2)] * 3
    )


@pytest.mark.parametrize(("profiles", "repeats"), [(("nobody",), 1), (("presence",), 0)])
def test_bad_trial_plans_are_refused(profiles: tuple[str, ...], repeats: int) -> None:
    with pytest.raises(ValueError):
        plan_trials(CORPUS.cases, profiles, repeats)


def test_each_read_is_recorded_with_its_kinds_tokens_and_cost(tmp_path: Path) -> None:
    script = [
        findings("non_compete", "non_compete", "unlimited_liability"),
        findings(),
        findings("ip_assignment"),
    ]
    done, trials, _ = run(tmp_path, counting(script))
    assert done and [t["status"] for t in trials] == ["ok", "ok", "ok"]
    first = trials[0]
    assert first["flagged"] == ["non_compete", "unlimited_liability"]  # sorted, once each
    assert (first["attempts"], first["calls"]) == (1, 1)
    assert (first["input_tokens"], first["output_tokens"]) == (10, 5)
    assert first["cost_usd"] == "0.00007" and first["hit_output_cap"] is False


def test_each_profile_sends_its_own_instructions(tmp_path: Path) -> None:
    fake = FakeProvider([findings()] * 2)
    provider = CountingProvider(fake, Budget(cap=Decimal(1), price=PRICE))
    writer = ResultsWriter(tmp_path)
    trials = plan_trials(CORPUS.cases[:1], ("presence", "production"), 1)
    run_cuad(FIXTURE, trials, provider=provider, profiles=PROFILES, writer=writer)
    presence, production = (r.system for r in fake.requests)
    assert "whichever party they favor" in presence and "Acme Corp" in production


def test_a_reader_that_never_answers_validly_is_charged_and_recorded(tmp_path: Path) -> None:
    provider = counting(["not json", "still not json", findings(), findings()])
    done, trials, _ = run(tmp_path, provider)
    assert done and trials[0]["status"] == "reader_error" and trials[0]["flagged"] == []
    assert trials[0]["calls"] == 2 and trials[0]["cost_usd"] == "0.00014"


def test_an_outage_is_retried_with_backoff_then_recorded(tmp_path: Path) -> None:
    down = LLMUnavailableError("503")
    provider = counting([down, down, down, down, findings(), findings()])
    done, trials, sleeps = run(tmp_path, provider)
    assert [t["status"] for t in trials] == ["unavailable", "ok", "ok"]
    assert sleeps == [2.0, 4.0, 2.0]  # two retries for the first contract, one for the second
    assert trials[0]["calls"] == 0 and trials[0]["cost_usd"] == "0"


class Expensive:
    """Reports 100,000 input tokens per call ($0.20 at $2 per million)."""

    def __init__(self, output_tokens: int = 5) -> None:
        self.output_tokens = output_tokens

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=json.dumps(findings()),
            model="test-model",
            input_tokens=100_000,
            output_tokens=self.output_tokens,
        )


def test_the_budget_stops_the_run_and_marks_the_rest(tmp_path: Path) -> None:
    budget = Budget(cap=Decimal("0.65"), price=PRICE)
    provider = CountingProvider(Expensive(), budget, input_tokens=lambda r: 100_000)
    done, trials, _ = run(tmp_path, provider, repeats=2)
    assert not done
    assert [t["status"] for t in trials] == ["ok"] * 3 + ["budget_stopped"] * 3
    assert budget.spent <= budget.cap and budget.calls == 3


def test_hitting_the_output_cap_is_recorded(tmp_path: Path) -> None:
    budget = Budget(cap=Decimal(10), price=PRICE)
    provider = CountingProvider(Expensive(output_tokens=2048), budget)
    _, trials, _ = run(tmp_path, provider)
    assert all(t["hit_output_cap"] for t in trials)


def test_results_never_contain_contract_text_or_model_output(tmp_path: Path) -> None:
    run(tmp_path, counting([findings("non_compete")] * 3))
    written = (tmp_path / "out" / "trials.jsonl").read_text()
    for phrase in ("DISTRIBUTOR AGREEMENT", "Northwind", "synthetic test text", "A contract."):
        assert phrase not in written
