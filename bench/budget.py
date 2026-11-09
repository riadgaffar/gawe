"""Spend control: prices, the per-call meter, and the rules for whether a run may start.

Three layers keep spend under the cap (`--max-usd`):

1. Before starting, `start_decision` compares the estimate with the cap (section 4 of the
   design): input cost alone over the cap always refuses; otherwise the expected total (from a
   paid trial) or, without one, the hard ceiling must fit, unless the ceiling was knowingly
   accepted.
2. Before every model call, `Budget.check` refuses if the call's worst case (input plus its full
   `max_output_tokens`) could take spend past the cap. The run then stops cleanly.
3. After every call, `Budget.charge` adds what the API actually reported. Calls whose answer the
   Reader later rejects are charged too: `CountingProvider` sits under the Reader, so it sees
   every attempt (`Reader.read` itself only reports its last one). A call where the model stopped
   early (a refusal, or running out of `max_output_tokens`) is billed but reaches us as an error
   with no usage, so it is charged at its worst case. The Reader and Planner treat it as a
   failed attempt; each trial record counts them (`early_stops`).

Thinking tokens are billed as output and count toward `max_output_tokens`, so the worst case
holds for models that think.
"""

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from gawe.llm.provider import (
    LLMOutputError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderConfigError,
)

PRICES_FILE = Path(__file__).parent / "prices.yaml"
INPUT_MARGIN = Decimal("1.05")  # the token-counting endpoint calls its count an estimate
_MILLION = Decimal(1_000_000)
_SHOWN = Decimal("0.0001")  # dollars are shown to a hundredth of a cent


class UnknownModelError(ValueError):
    """The model has no row in the price table."""


class BudgetExceededError(Exception):
    """The next call could take spend past the cap; the run must stop."""


@dataclass(frozen=True)
class Price:
    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    checked_on: date
    source: str

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts cannot be negative")
        return (
            Decimal(input_tokens) * self.input_per_mtok
            + Decimal(output_tokens) * self.output_per_mtok
        ) / _MILLION

    def as_manifest(self) -> dict[str, str]:
        return {
            "model": self.model,
            "input_per_mtok": str(self.input_per_mtok),
            "output_per_mtok": str(self.output_per_mtok),
            "checked_on": self.checked_on.isoformat(),
            "source": self.source,
        }


def load_prices(path: Path = PRICES_FILE) -> dict[str, Price]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise ValueError(f"{path.name} must have a 'models' mapping")
    checked_on = data.get("checked_on")
    if not isinstance(checked_on, date):
        raise ValueError(f"{path.name} must have a 'checked_on' date")
    source = str(data.get("source", ""))
    prices: dict[str, Price] = {}
    for model, row in data["models"].items():
        if not isinstance(row, Mapping):
            raise ValueError(f"{path.name}: {model} must have input and output prices")
        prices[str(model)] = Price(
            model=str(model),
            input_per_mtok=_positive(row.get("input"), f"{model} input"),
            output_per_mtok=_positive(row.get("output"), f"{model} output"),
            checked_on=checked_on,
            source=source,
        )
    return prices


def price_for(model: str, prices: Mapping[str, Price] | None = None) -> Price:
    table = load_prices() if prices is None else prices
    try:
        return table[model]
    except KeyError:
        known = ", ".join(sorted(table)) or "none"
        raise UnknownModelError(
            f"no price for model {model!r} in bench/prices.yaml (known: {known})"
        ) from None


def parse_usd(text: str) -> Decimal:
    """A spend cap from the command line: a positive amount of dollars."""
    return _positive(text, "--max-usd")


def _positive(value: Any, what: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{what} must be a number of dollars") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{what} must be more than zero")
    return amount


def estimate_input_tokens(request: LLMRequest) -> int:
    """A cautious stand-in when no exact count is known: one token per character.

    Real counts are several times lower, so this only makes the per-call check stop a little early.
    The free count (step B5) supplies exact numbers instead.
    """
    return len(request.system) + len(request.user)


@dataclass
class Budget:
    """What has been spent against a cap, safe to share between threads."""

    cap: Decimal
    price: Price
    spent: Decimal = Decimal(0)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.cap <= 0:
            raise ValueError("the spend cap must be more than zero")

    @property
    def remaining(self) -> Decimal:
        return self.cap - self.spent

    def worst_case(self, input_tokens: int, max_output_tokens: int) -> Decimal:
        return self.price.cost(input_tokens, max_output_tokens)

    def check(self, input_tokens: int, max_output_tokens: int) -> None:
        """Raise BudgetExceededError unless this call fits even if it uses all its output."""
        worst = self.worst_case(input_tokens, max_output_tokens)
        with self._lock:
            if self.spent + worst > self.cap:
                raise BudgetExceededError(
                    f"next call could cost up to ${_usd(worst)}; ${_usd(self.remaining)} left"
                )

    def charge(self, input_tokens: int, output_tokens: int) -> Decimal:
        amount = self.price.cost(input_tokens, output_tokens)
        with self._lock:
            self.spent += amount
            self.calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
        return amount


@dataclass(frozen=True)
class CallRecord:
    purpose: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    hit_output_cap: bool
    early_stop: bool = False  # the model stopped before answering; cost charged at worst case
    model: str = ""  # as the API reported it


class CountingProvider:
    """Wraps a provider: checks the budget before each call and charges it after."""

    def __init__(
        self,
        inner: LLMProvider,
        budget: Budget,
        *,
        input_tokens: Callable[[LLMRequest], int] = estimate_input_tokens,
    ) -> None:
        self._inner = inner
        self._budget = budget
        self._input_tokens = input_tokens
        self._lock = threading.Lock()
        self.records: list[CallRecord] = []  # since the last take_records()
        self.history: list[CallRecord] = []  # every call, never cleared
        self.stopped_by: Exception | None = None  # a budget or API refusal that ends the run

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            self._budget.check(self._input_tokens(request), request.max_output_tokens)
        except BudgetExceededError as exc:
            self.stopped_by = exc  # remembered: a caller may swallow the exception (see below)
            raise
        try:
            response = self._inner.complete(request)  # an outage before any reply is not billed
        except LLMOutputError:
            self._charge_early_stop(request)
            raise
        except ProviderConfigError as exc:
            # The API refused the request itself (no credit, bad key, unknown model). Nothing is
            # billed, and nothing later will succeed, so the run must stop rather than carry on.
            self.stopped_by = exc
            raise
        cost = self._budget.charge(response.input_tokens, response.output_tokens)
        record = CallRecord(
            purpose=request.purpose,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=cost,
            hit_output_cap=response.output_tokens >= request.max_output_tokens,
            model=response.model,
        )
        self._keep(record)
        return response

    def _keep(self, record: CallRecord) -> None:
        with self._lock:
            self.records.append(record)
            self.history.append(record)

    def _charge_early_stop(self, request: LLMRequest) -> None:
        """The model stopped early (refusal or `max_tokens`): billed, but the provider raises
        instead of returning usage. Charge the worst case so spend is never under-counted."""
        input_tokens = self._input_tokens(request)
        cost = self._budget.charge(input_tokens, request.max_output_tokens)
        record = CallRecord(
            purpose=request.purpose,
            input_tokens=input_tokens,
            output_tokens=request.max_output_tokens,
            cost_usd=cost,
            hit_output_cap=True,
            early_stop=True,
        )
        self._keep(record)

    def raise_if_stopped(self) -> None:
        """Re-raise a budget or API refusal that something between here and the caller swallowed.

        The API service catches any exception while advancing a run, so the run can be resumed
        later; without this, a stop inside a run would look like a stalled run.
        """
        if self.stopped_by is not None:
            raise self.stopped_by

    def take_records(self) -> list[CallRecord]:
        """The calls since the last take, for attributing cost to one trial."""
        with self._lock:
            taken, self.records = self.records, []
        return taken


@dataclass(frozen=True)
class Estimate:
    """What a run will cost, in dollars. `expected_usd` exists only after a paid trial."""

    input_usd: Decimal
    ceiling_usd: Decimal
    expected_usd: Decimal | None = None


@dataclass(frozen=True)
class StartDecision:
    ok: bool
    reason: str


def start_decision(
    estimate: Estimate, cap: Decimal, *, accept_ceiling_over_cap: bool = False
) -> StartDecision:
    """Whether a run may start under `cap` (design section 4)."""
    if estimate.input_usd > cap:
        return StartDecision(
            False,
            f"input alone costs ${_usd(estimate.input_usd)}, over the ${_usd(cap)} cap; "
            "the run could not finish. Use a smaller sample or a higher cap",
        )
    if estimate.expected_usd is not None:
        if estimate.expected_usd > cap:
            return StartDecision(
                False,
                f"expected total ${_usd(estimate.expected_usd)} is over the ${_usd(cap)} cap",
            )
        return StartDecision(True, f"expected ${_usd(estimate.expected_usd)} of ${_usd(cap)}")
    if estimate.ceiling_usd > cap and not accept_ceiling_over_cap:
        return StartDecision(
            False,
            f"the worst case ${_usd(estimate.ceiling_usd)} is over the ${_usd(cap)} cap. Run a "
            "paid trial (--trial) for an expected total, or pass --accept-ceiling-over-cap to "
            "rely on the per-call stop",
        )
    return StartDecision(True, f"worst case ${_usd(estimate.ceiling_usd)} of ${_usd(cap)}")


def _usd(amount: Decimal) -> str:
    return str(amount.quantize(_SHOWN))
