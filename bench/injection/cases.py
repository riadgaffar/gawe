"""Loads and checks the injection suite (cases.yaml and the corpus documents).

Everything is validated up front: unknown bases, goals or techniques, a goal used with an app it
does not belong to, a technique missing the text it needs, and malformed matchers all stop the
load, so a typo cannot silently turn into an attack that never fires.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bench.injection.matchers import Matcher, check_matcher
from bench.injection.techniques import FIXED, REQUIRES, TECHNIQUES, render

HERE = Path(__file__).parent
CASES_FILE = HERE / "cases.yaml"
CORPUS_DIR = HERE / "corpus"
APPS = ("contract_review", "it_access")
POLICY_OUTCOMES = ("allow", "needs_approval", "deny")
EFFECT_KINDS = ("email.sent", "crm.assessment", "esign.envelope", "iam.grant")


class SuiteError(ValueError):
    pass


@dataclass(frozen=True)
class Base:
    name: str
    app: str
    file: str
    objective: str
    expect_effect: str


@dataclass(frozen=True)
class Goal:
    name: str
    app: str
    policy: str  # what policy as written does with the attacker's step
    plan_tool: str
    plan_args: Matcher
    effect_kind: str
    effect_match: Matcher
    findings: Matcher | None
    text: Mapping[str, Any] = field(repr=False)  # payload, payload_es, parts, tool_call


@dataclass(frozen=True)
class Case:
    case_id: str
    base: Base
    technique: str  # "none" for a clean twin
    goal: Goal | None = None
    canary: str | None = None
    document_file: str | None = None  # fixed text instead of a generated one

    @property
    def is_twin(self) -> bool:
        return self.goal is None

    @property
    def app(self) -> str:
        return self.base.app

    @property
    def objective(self) -> str:
        return self.base.objective


@dataclass(frozen=True)
class Suite:
    attacks: tuple[Case, ...]
    twins: tuple[Case, ...]
    goals: Mapping[str, Goal]

    @property
    def cases(self) -> tuple[Case, ...]:
        return self.attacks + self.twins


def canary_for(case_id: str) -> str:
    return "REF-" + hashlib.sha256(case_id.encode()).hexdigest()[:8].upper()


def load_suite(path: Path = CASES_FILE, corpus: Path = CORPUS_DIR) -> Suite:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SuiteError("cases.yaml must be a mapping")
    bases = {name: _base(name, raw, corpus) for name, raw in _section(data, "bases").items()}
    goals = {name: _goal(name, raw) for name, raw in _section(data, "goals").items()}
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SuiteError("cases.yaml needs a list of cases")
    attacks = [_case(raw, bases, goals, corpus) for raw in raw_cases]
    ids = [c.case_id for c in attacks]
    if len(set(ids)) != len(ids):
        raise SuiteError("case ids must be unique")
    twins = [Case(case_id=f"twin-{b.name}", base=b, technique="none") for b in bases.values()]
    return Suite(attacks=tuple(attacks), twins=tuple(twins), goals=goals)


def document_for(case: Case, corpus: Path = CORPUS_DIR) -> str:
    if case.document_file is not None:
        return (corpus / case.document_file).read_text(encoding="utf-8")
    base = (corpus / case.base.file).read_text(encoding="utf-8")
    if case.goal is None or case.canary is None:
        return base
    return render(case.technique, base, case.goal.text, case.canary)


def corpus_sha256(path: Path = CASES_FILE, corpus: Path = CORPUS_DIR) -> str:
    """Identifies the exact suite in a run's manifest."""
    digest = hashlib.sha256(path.read_bytes())
    for file in sorted(p for p in corpus.rglob("*") if p.is_file()):
        digest.update(file.relative_to(corpus).as_posix().encode() + b"\0" + file.read_bytes())
    return digest.hexdigest()


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = data.get(name)
    if not isinstance(section, dict) or not section:
        raise SuiteError(f"cases.yaml needs a '{name}' mapping")
    return section


def _base(name: str, raw: Any, corpus: Path) -> Base:
    if not isinstance(raw, dict):
        raise SuiteError(f"base {name}: must be a mapping")
    base = Base(
        name=name,
        app=str(raw.get("app")),
        file=str(raw.get("file")),
        objective=str(raw.get("objective", "")).strip(),
        expect_effect=str(raw.get("expect_effect")),
    )
    if base.app not in APPS:
        raise SuiteError(f"base {name}: unknown app {base.app!r}")
    if not (corpus / base.file).is_file():
        raise SuiteError(f"base {name}: no document at {base.file}")
    if not base.objective or base.expect_effect not in EFFECT_KINDS:
        raise SuiteError(f"base {name}: needs an objective and a known expect_effect")
    return base


def _goal(name: str, raw: Any) -> Goal:
    if not isinstance(raw, dict):
        raise SuiteError(f"goal {name}: must be a mapping")
    try:
        plan, effect = raw["plan"], raw["effect"]
        goal = Goal(
            name=name,
            app=str(raw["app"]),
            policy=str(raw["policy"]),
            plan_tool=str(plan["tool"]),
            plan_args=plan["args"],
            effect_kind=str(effect["kind"]),
            effect_match=effect["match"],
            findings=raw.get("findings"),
            text={k: raw[k] for k in ("payload", "payload_es", "parts", "tool_call") if k in raw},
        )
        check_matcher(goal.plan_args, f"goal {name} plan")
        check_matcher(goal.effect_match, f"goal {name} effect")
        if goal.findings is not None:
            check_matcher(goal.findings, f"goal {name} findings")
    except (KeyError, TypeError, ValueError) as exc:
        raise SuiteError(f"goal {name}: {exc}") from exc
    if goal.app not in APPS or goal.policy not in POLICY_OUTCOMES:
        raise SuiteError(f"goal {name}: unknown app or policy outcome")
    if goal.effect_kind not in EFFECT_KINDS:
        raise SuiteError(f"goal {name}: unknown effect kind {goal.effect_kind!r}")
    if "{canary}" not in str(goal.text.get("payload", "")):
        raise SuiteError(f"goal {name}: the payload must carry {{canary}}")
    parts = goal.text.get("parts")
    if parts is not None and (not isinstance(parts, list) or len(parts) != 2):
        raise SuiteError(f"goal {name}: parts must be two strings")
    return goal


def _case(raw: Any, bases: Mapping[str, Base], goals: Mapping[str, Goal], corpus: Path) -> Case:
    if not isinstance(raw, dict) or "id" not in raw:
        raise SuiteError("every case needs an id")
    case_id = str(raw["id"])
    base, goal = bases.get(str(raw.get("base"))), goals.get(str(raw.get("goal")))
    technique = str(raw.get("technique"))
    if base is None or goal is None:
        raise SuiteError(f"case {case_id}: unknown base or goal")
    if base.app != goal.app:
        raise SuiteError(f"case {case_id}: goal {goal.name} is for {goal.app}, not {base.app}")
    document = raw.get("document")
    if document is not None:
        if technique != FIXED or not (corpus / str(document)).is_file():
            raise SuiteError(f"case {case_id}: a fixed document needs technique '{FIXED}'")
        return Case(case_id, base, technique, goal, None, str(document))
    if technique not in TECHNIQUES:
        raise SuiteError(f"case {case_id}: unknown technique {technique!r}")
    needed = REQUIRES.get(technique)
    if needed is not None and needed not in goal.text:
        raise SuiteError(f"case {case_id}: {technique} needs '{needed}' in goal {goal.name}")
    return Case(case_id, base, technique, goal, canary_for(case_id))
