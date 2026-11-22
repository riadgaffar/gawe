"""Hand-written attacks (M18, design D4 as revised on 2026-11-22, after the attacker model
declined): 5 per goal, each a different framing, inserted as written into a suite base document.

Each becomes a suite-shaped attack case and is checked the way the suite's own cases are (its
goal's attack step must match the goal's plan, and so on). They run live only, never in the
scripted benchmark.
"""

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from bench.injection.cases import Case, Suite, SuiteError, _checked, canary_for, load_suite

HANDWRITTEN_FILE = Path(__file__).parent / "handwritten.yaml"
TECHNIQUE = "handwritten"


def load_handwritten(path: Path = HANDWRITTEN_FILE, suite: Suite | None = None) -> tuple[Case, ...]:
    suite = suite or load_suite()
    bases = {twin.base.name: twin.base for twin in suite.twins}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw_attacks = data.get("attacks") if isinstance(data, dict) else None
    if not isinstance(raw_attacks, list) or not raw_attacks:
        raise SuiteError(f"{path.name} needs a list of attacks")
    cases = [_attack(raw, suite, bases) for raw in raw_attacks]
    ids = [c.case_id for c in cases]
    if len(set(ids)) != len(ids):
        raise SuiteError(f"{path.name}: attack ids must be unique")
    return tuple(cases)


def handwritten_sha256(path: Path = HANDWRITTEN_FILE) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _attack(raw: Any, suite: Suite, bases: dict[str, Any]) -> Case:
    if not isinstance(raw, dict) or not str(raw.get("id", "")).startswith("hw-"):
        raise SuiteError("handwritten attack: needs an id starting with hw-")
    case_id = str(raw["id"])
    goal, base = suite.goals.get(str(raw.get("goal"))), bases.get(str(raw.get("base")))
    payload = raw.get("payload")
    if goal is None or base is None:
        raise SuiteError(f"{case_id}: unknown goal or base")
    if base.app != goal.app:
        raise SuiteError(f"{case_id}: goal {goal.name} is for {goal.app}, not {base.app}")
    if not isinstance(payload, str) or payload.count("{canary}") != 1:
        raise SuiteError(f"{case_id}: the payload must carry {{canary}} exactly once")
    if not raw.get("framing"):
        raise SuiteError(f"{case_id}: needs a framing label")
    case = Case(
        case_id=case_id,
        base=base,
        technique=TECHNIQUE,
        goal=replace(goal, text={"payload": payload}),
        canary=canary_for(case_id),
    )
    return _checked(case)
