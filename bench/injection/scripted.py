"""Scripted runs: the injection suite with a model that is fooled on purpose, and no model calls.

The live benchmark measures how often a real model is fooled. These runs measure the other half:
what plan validation, policy and approvals do once it *is* fooled. Each attack goes through the
same engine as a live trial (`drive`: real Reader and Planner code, plan validator, policy engine,
executor, mock systems, never approving), but the model's replies are written here:

- `planner`: the Reader reports the base document's clean findings; the Planner proposes the honest
  plan for them with the attacker's step added (or, for a goal marked `replaces_honest_step`, in
  place of the honest step that uses the same tool).
- `reader`: the Reader reports the attacker's findings; the Planner faithfully plans from them.
  Only goals with `attack_findings` can be reached this way, since the Reader's schema has no
  field for an email recipient or a signer.

A third mode, both fooled, is left out: for every goal the Reader can reach, the honest plan built
from fooled findings already is the attacker's step, so it would repeat the `reader` rows.

The replies are fixed, so each (case, mode) runs once. Nothing here can reach a real model: the
provider is a local script behind a zero-price meter with a one-millionth-of-a-dollar cap.
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from bench.budget import Budget, CountingProvider, Price
from bench.injection.cases import Case, Suite, attack_step_for, fooled_findings_for
from bench.injection.run import drive
from bench.results import ResultsWriter, run_trials
from gawe.llm.provider import LLMRequest, LLMResponse

KIND = "injection_scripted"
MODES = ("planner", "reader")
SCRIPTED_MODEL = "scripted"
_TINY_CAP = Decimal("0.000001")
_FREE = Price(SCRIPTED_MODEL, Decimal(0), Decimal(0), date(2026, 11, 9), "no model is called")


@dataclass(frozen=True)
class ScriptedTrial:
    case: Case
    mode: str


def scripted_trials(suite: Suite) -> list[ScriptedTrial]:
    """Every applicable (attack, mode) pair, mode-major."""
    return [
        ScriptedTrial(case, mode)
        for mode in MODES
        for case in suite.attacks
        if mode == "planner" or fooled_findings_for(case) is not None
    ]


def replies_for(case: Case, mode: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The Reader's findings and the Planner's plan for this case and mode."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if mode == "reader":
        fooled = fooled_findings_for(case)
        if fooled is None:
            raise ValueError(f"{case.case_id}: goal cannot be reached through the Reader")
        return fooled, honest_plan(case, fooled)
    findings = dict(case.base.clean_findings)
    return findings, with_attack(case, honest_plan(case, findings))


def honest_plan(case: Case, findings: Mapping[str, Any]) -> dict[str, Any]:
    """What a Planner that is not fooled proposes for these findings."""
    if case.app == "contract_review":
        contract = case.base.contract_id
        return {
            "steps": [
                {"id": "get", "tool": "clm.get_contract", "args": {"contract_id": contract}},
                {
                    "id": "assess",
                    "tool": "crm.write_assessment",
                    "args": {
                        "contract_id": contract,
                        "risk_level": findings["risk_level"],
                        "summary": str(findings["summary"])[:500],
                    },
                    "depends_on": ["get"],
                },
            ]
        }
    if case.app == "it_access":
        who = findings["requester_email"]
        return {
            "steps": [
                {"id": "lookup", "tool": "iam.lookup_user", "args": {"email": who}},
                {
                    "id": "grant",
                    "tool": "iam.grant_access",
                    "args": {
                        "email": who,
                        "system": findings["system"],
                        "level": findings["level"],
                    },
                    "depends_on": ["lookup"],
                },
            ]
        }
    raise ValueError(f"no honest plan for app {case.app!r}")


def with_attack(case: Case, plan: Mapping[str, Any]) -> dict[str, Any]:
    """The plan with the attacker's step added after the last step, or replacing its namesake."""
    assert case.goal is not None
    attack = attack_step_for(case)
    steps = [dict(s) for s in plan["steps"]]
    if case.goal.replaces_honest_step:
        index = next(i for i, s in enumerate(steps) if s["tool"] == attack["tool"])
        steps[index] = {**steps[index], "args": attack["args"]}
    else:
        steps.append({"id": "attack", **attack, "depends_on": [steps[-1]["id"]]})
    return {"steps": steps}


class ScriptedModel:
    """Answers every Reader call with `findings` and every Planner call with `plan`."""

    def __init__(self, findings: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
        self._replies = {"read": json.dumps(findings), "plan": json.dumps(plan)}

    def complete(self, request: LLMRequest) -> LLMResponse:
        text = self._replies.get(request.purpose)
        if text is None:
            raise AssertionError(f"scripted model has no reply for {request.purpose!r}")
        return LLMResponse(text=text, model=SCRIPTED_MODEL, input_tokens=0, output_tokens=0)


def metered(model: ScriptedModel) -> CountingProvider:
    """The scripted model behind the same meter a live run uses (`drive` needs one)."""
    # The meter needs a positive cap; at a zero price nothing is ever charged against it.
    return CountingProvider(model, Budget(cap=_TINY_CAP, price=_FREE))


def scripted_provider(case: Case, mode: str) -> CountingProvider:
    return metered(ScriptedModel(*replies_for(case, mode)))


def run_one(trial: ScriptedTrial) -> dict[str, Any]:
    case = trial.case
    record = drive(case, scripted_provider(case, trial.mode), _no_sleep)
    for field in ("reader_fooled", "planner_fooled", "model_fooled"):
        record.pop(field, None)  # scripted, not measured: the mode says who was fooled
    return {
        "case_id": case.case_id,
        "mode": trial.mode,
        "app": case.app,
        "goal": case.goal.name if case.goal else None,
        "technique": case.technique,
        **record,
    }


def run_scripted(trials: Sequence[ScriptedTrial], *, writer: ResultsWriter) -> bool:
    """Run every trial and write one record each. Returns True (nothing can stop it early)."""
    return run_trials(trials, run_one, writer=writer, kind=KIND, stopped=_never_stopped)


def iter_results(trials: Sequence[ScriptedTrial]) -> Iterator[dict[str, Any]]:
    """The records without writing them (for tests)."""
    return (run_one(t) for t in trials)


def _no_sleep(seconds: float) -> None:
    raise AssertionError("a scripted model is never unavailable")


def _never_stopped(trial: ScriptedTrial) -> dict[str, Any]:
    raise AssertionError("a scripted run has no budget to stop it")
