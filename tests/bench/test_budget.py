from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bench.budget import (
    INPUT_MARGIN,
    Budget,
    BudgetExceededError,
    CountingProvider,
    Estimate,
    Price,
    UnknownModelError,
    estimate_input_tokens,
    load_prices,
    parse_usd,
    price_for,
    start_decision,
)
from gawe.llm.fake import FakeProvider
from gawe.llm.provider import LLMRequest, LLMResponse, LLMUnavailableError
from gawe.llm.reader import Reader, ReaderError
from tests.unit.test_llm_reader import PROFILE

# $2 / $10 per million tokens: one input token is $0.000002, one output token $0.00001.
PRICE = Price("test-model", Decimal("2"), Decimal("10"), date(2026, 10, 29), "test")


def request(user: str = "x" * 100, max_output_tokens: int = 1000) -> LLMRequest:
    return LLMRequest(
        purpose="read", system="", user=user, output_schema={}, max_output_tokens=max_output_tokens
    )


class Scripted:
    """A provider whose replies report chosen token counts."""

    def __init__(self, *usage: tuple[int, int], text: str = "{}") -> None:
        self.usage = list(usage)
        self.text = text
        self.calls = 0

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        inp, out = self.usage.pop(0)
        return LLMResponse(text=self.text, model="test-model", input_tokens=inp, output_tokens=out)


# ----------------------------------------------------------------------- prices


def test_the_shipped_price_table_has_the_planned_models() -> None:
    prices = load_prices()
    assert prices["claude-sonnet-5-5"].input_per_mtok == Decimal("2.00")
    assert prices["claude-sonnet-5-5"].output_per_mtok == Decimal("10.00")
    assert prices["claude-opus-5-5"].output_per_mtok == Decimal("20.00")
    assert all(p.checked_on == date(2026, 10, 29) for p in prices.values())


def test_an_unknown_model_is_refused_rather_than_guessed() -> None:
    with pytest.raises(UnknownModelError, match="no price for model 'gpt-x'"):
        price_for("gpt-x")


def test_cost_is_exact_decimal_arithmetic() -> None:
    assert PRICE.cost(1_000_000, 0) == Decimal(2)
    assert PRICE.cost(0, 1_000_000) == Decimal(10)
    assert PRICE.cost(3, 7) == Decimal("0.000076")
    with pytest.raises(ValueError):
        PRICE.cost(-1, 0)


@pytest.mark.parametrize(
    "body",
    [
        "checked_on: 2026-10-29\n",
        "models: {}\n",
        "checked_on: 2026-10-29\nmodels:\n  m: 3\n",
        "checked_on: 2026-10-29\nmodels:\n  m: {input: '0', output: '1'}\n",
        "checked_on: 2026-10-29\nmodels:\n  m: {input: 'free', output: '1'}\n",
    ],
)
def test_a_broken_price_table_is_refused(tmp_path: Path, body: str) -> None:
    path = tmp_path / "prices.yaml"
    path.write_text(body)
    with pytest.raises(ValueError):
        load_prices(path)


@pytest.mark.parametrize("text", ["0", "-5", "nan", "inf", "five", ""])
def test_a_cap_must_be_a_positive_amount(text: str) -> None:
    with pytest.raises(ValueError):
        parse_usd(text)


def test_a_valid_cap_is_kept_exactly() -> None:
    assert parse_usd("2.50") == Decimal("2.50")


# ----------------------------------------------------------------------- budget


def test_a_call_that_fits_exactly_is_allowed_and_one_cent_more_is_not() -> None:
    # Worst case of 100 input + 1000 output tokens is $0.0102.
    budget = Budget(cap=Decimal("0.0102"), price=PRICE)
    budget.check(100, 1000)
    budget.charge(1, 0)  # now $0.000002 spent: the same call no longer fits
    with pytest.raises(BudgetExceededError):
        budget.check(100, 1000)


def test_charge_adds_up_tokens_calls_and_dollars() -> None:
    budget = Budget(cap=Decimal(1), price=PRICE)
    budget.charge(100, 10)
    budget.charge(200, 20)
    assert (budget.calls, budget.input_tokens, budget.output_tokens) == (2, 300, 30)
    assert budget.spent == Decimal("0.0009") and budget.remaining == Decimal("0.9991")


def test_a_budget_needs_a_positive_cap() -> None:
    with pytest.raises(ValueError):
        Budget(cap=Decimal(0), price=PRICE)


def test_the_default_input_estimate_is_one_token_per_character() -> None:
    req = LLMRequest(purpose="read", system="abc", user="defgh", output_schema={})
    assert estimate_input_tokens(req) == 8


# --------------------------------------------------------------------- provider


def test_counting_provider_charges_what_the_api_reported() -> None:
    budget = Budget(cap=Decimal(1), price=PRICE)
    provider = CountingProvider(Scripted((500, 40)), budget)
    provider.complete(request())
    (record,) = provider.take_records()
    assert (record.input_tokens, record.output_tokens) == (500, 40)
    assert record.cost_usd == Decimal("0.0014") and budget.spent == Decimal("0.0014")
    assert not record.hit_output_cap and provider.take_records() == []


def test_counting_provider_refuses_before_calling_when_the_worst_case_does_not_fit() -> None:
    inner = Scripted((1, 1))
    budget = Budget(cap=Decimal("0.001"), price=PRICE)  # worst case of the request is $0.0102
    with pytest.raises(BudgetExceededError):
        CountingProvider(inner, budget).complete(request())
    assert inner.calls == 0 and budget.spent == 0


def test_counting_provider_uses_an_exact_count_when_given() -> None:
    budget = Budget(cap=Decimal("0.0100"), price=PRICE)
    provider = CountingProvider(Scripted((0, 0)), budget, input_tokens=lambda req: 0)
    provider.complete(request(max_output_tokens=1000))  # worst case exactly $0.01: allowed


def test_hitting_the_output_cap_is_recorded() -> None:
    provider = CountingProvider(Scripted((10, 1000)), Budget(cap=Decimal(1), price=PRICE))
    provider.complete(request(max_output_tokens=1000))
    assert provider.take_records()[0].hit_output_cap


def test_an_outage_before_any_reply_is_not_charged() -> None:
    budget = Budget(cap=Decimal(1), price=PRICE)
    provider = CountingProvider(FakeProvider([LLMUnavailableError("down")]), budget)
    with pytest.raises(LLMUnavailableError):
        provider.complete(request())
    assert budget.spent == 0 and provider.take_records() == []


def test_failed_reader_attempts_are_charged_too() -> None:
    """Reader.read only reports its last attempt; the meter sees both."""
    budget = Budget(cap=Decimal(1), price=PRICE)
    inner = Scripted((1000, 200), (1000, 200), text="not json")
    reader = Reader(CountingProvider(inner, budget), PROFILE, max_attempts=2)
    with pytest.raises(ReaderError):
        reader.read("a contract")
    assert inner.calls == 2 and budget.calls == 2 and budget.spent == Decimal("0.008")


# ------------------------------------------------------------------ start rules

CAP = Decimal(10)


def est(input_usd: str, ceiling: str, expected: str | None = None) -> Estimate:
    return Estimate(Decimal(input_usd), Decimal(ceiling), Decimal(expected) if expected else None)


@pytest.mark.parametrize(
    ("estimate", "kwargs", "ok", "says"),
    [
        (est("11", "20"), {}, False, "input alone"),
        (est("11", "20", "12"), {"accept_ceiling_over_cap": True}, False, "input alone"),
        (est("4", "30", "11"), {}, False, "expected total"),
        (est("4", "30", "9"), {}, True, "expected"),
        (est("4", "30"), {}, False, "worst case"),
        (est("4", "30"), {"accept_ceiling_over_cap": True}, True, "worst case"),
        (est("4", "10"), {}, True, "worst case"),
    ],
)
def test_start_decision_follows_the_design(
    estimate: Estimate, kwargs: dict[str, Any], ok: bool, says: str
) -> None:
    decision = start_decision(estimate, CAP, **kwargs)
    assert decision.ok is ok and says in decision.reason


def test_the_input_margin_is_five_percent() -> None:
    assert Decimal("1.05") == INPUT_MARGIN
