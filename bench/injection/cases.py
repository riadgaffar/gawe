"""Loads and checks the injection suite (cases.yaml and the corpus documents).

Everything is validated up front: unknown bases, goals or techniques, a goal used with an app it
does not belong to, a technique missing the text it needs, and malformed matchers all stop the
load, so a typo cannot silently turn into an attack that never fires.

The values scripted runs use are checked the same way, per case: the attacker's step must match
the goal's plan matcher, fooled findings must match its findings matcher and the app's Reader
schema, and the base's clean findings must not already satisfy the goal (an attack an honest run
would "carry out" anyway measures nothing).
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from bench.injection.matchers import Matcher, check_matcher, matches, step_matches
from bench.injection.techniques import FIXED, REQUIRES, TECHNIQUES, render
from gawe.apps.contract_review.reader_schema import SCHEMA as CONTRACT_REVIEW_SCHEMA
from gawe.apps.it_access.reader_schema import SCHEMA as IT_ACCESS_SCHEMA

HERE = Path(__file__).parent
CASES_FILE = HERE / "cases.yaml"
CORPUS_DIR = HERE / "corpus"
APPS = ("contract_review", "it_access")
POLICY_OUTCOMES = ("allow", "needs_approval", "deny")
EFFECT_KINDS = ("email.sent", "crm.assessment", "esign.envelope", "iam.grant")
READER_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    "contract_review": CONTRACT_REVIEW_SCHEMA,
    "it_access": IT_ACCESS_SCHEMA,
}
PLACEHOLDERS = ("{canary}", "{contract_id}", "{requester_email}")
SCRIPTED_MODES = ("planner", "reader")
HONEST_TOOLS = {  # the steps of an honest plan in each app (scripted.honest_plan)
    "contract_review": ("clm.get_contract", "crm.write_assessment"),
    "it_access": ("iam.lookup_user", "iam.grant_access"),
}
SCRIPTED_OUTCOMES = ("none", "policy_deny", "approval_pending", "validator", "not_run")


class SuiteError(ValueError):
    pass


@dataclass(frozen=True)
class Base:
    name: str
    app: str
    file: str
    objective: str
    expect_effect: str
    clean_findings: Mapping[str, Any] = field(default_factory=dict, repr=False)
    contract_id: str | None = None


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
    attack_step: Mapping[str, Any] = field(default_factory=dict, repr=False)
    attack_findings: Mapping[str, Any] | None = field(default=None, repr=False)
    replaces_honest_step: bool = False  # the attack changes an honest step instead of adding one
    expect_scripted: Mapping[str, str] = field(default_factory=dict)  # mode -> stopped_by


@dataclass(frozen=True)
class Case:
    case_id: str
    base: Base
    technique: str  # "none" for a clean twin
    goal: Goal | None = None
    canary: str | None = None
    document_file: str | None = None  # fixed text instead of a generated one
    expect_scripted: Mapping[str, str] = field(default_factory=dict)  # overrides the goal's

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


def attack_step_for(case: Case) -> dict[str, Any]:
    """The attacker's step for this case, as a fooled Planner would propose it."""
    if case.goal is None:
        raise ValueError(f"{case.case_id} is a clean twin")
    step: dict[str, Any] = _filled(dict(case.goal.attack_step), case)
    return step


def fooled_findings_for(case: Case) -> dict[str, Any] | None:
    """What a fooled Reader reports for this case, or None if the goal cannot reach the Reader."""
    if case.goal is None or case.goal.attack_findings is None:
        return None
    return {**case.base.clean_findings, **_filled(dict(case.goal.attack_findings), case)}


def _filled(value: Any, case: Case) -> Any:
    if isinstance(value, str):
        known = {
            "{canary}": case.canary or canary_for(case.case_id),
            "{contract_id}": case.base.contract_id,
            "{requester_email}": case.base.clean_findings.get("requester_email"),
        }
        for placeholder, replacement in known.items():
            if placeholder in value:
                if not isinstance(replacement, str):
                    raise SuiteError(f"case {case.case_id}: base has no value for {placeholder}")
                value = value.replace(placeholder, replacement)
        return value
    if isinstance(value, Mapping):
        return {k: _filled(v, case) for k, v in value.items()}
    if isinstance(value, list):
        return [_filled(v, case) for v in value]
    return value


def _schema_problem(app: str, findings: Mapping[str, Any]) -> str | None:
    error = next(iter(Draft202012Validator(READER_SCHEMAS[app]).iter_errors(findings)), None)
    return None if error is None else error.message


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
    clean = raw.get("clean_findings")
    contract_id = raw.get("contract_id")
    base = Base(
        name=name,
        app=str(raw.get("app")),
        file=str(raw.get("file")),
        objective=str(raw.get("objective", "")).strip(),
        expect_effect=str(raw.get("expect_effect")),
        clean_findings=clean if isinstance(clean, dict) else {},
        contract_id=None if contract_id is None else str(contract_id),
    )
    if base.app not in APPS:
        raise SuiteError(f"base {name}: unknown app {base.app!r}")
    if not isinstance(clean, dict):
        raise SuiteError(f"base {name}: needs clean_findings")
    problem = _schema_problem(base.app, clean)
    if problem is not None:
        raise SuiteError(f"base {name}: clean_findings do not fit the Reader schema: {problem}")
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
            attack_step=raw["attack_step"],
            attack_findings=raw.get("attack_findings"),
            replaces_honest_step=bool(raw.get("replaces_honest_step", False)),
            expect_scripted=raw["expect_scripted"],
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
    step = goal.attack_step
    if (
        not isinstance(step, dict)
        or step.get("tool") != goal.plan_tool
        or not isinstance(step.get("args"), dict)
    ):
        raise SuiteError(f"goal {name}: attack_step needs the plan's tool and an args mapping")
    if (goal.findings is None) != (goal.attack_findings is None):
        raise SuiteError(f"goal {name}: attack_findings go with a findings matcher, and only then")
    if goal.attack_findings is not None and not isinstance(goal.attack_findings, dict):
        raise SuiteError(f"goal {name}: attack_findings must be a mapping")
    modes = ("planner", "reader") if goal.attack_findings is not None else ("planner",)
    _check_expectations(goal.expect_scripted, modes, f"goal {name}")
    if "{canary}" not in str(goal.text.get("payload", "")):
        raise SuiteError(f"goal {name}: the payload must carry {{canary}}")
    parts = goal.text.get("parts")
    if parts is not None and (not isinstance(parts, list) or len(parts) != 2):
        raise SuiteError(f"goal {name}: parts must be two strings")
    return goal


def _check_expectations(expected: Any, modes: tuple[str, ...], where: str) -> None:
    if not isinstance(expected, dict) or set(expected) != set(modes):
        raise SuiteError(f"{where}: expect_scripted needs exactly the modes {list(modes)}")
    for mode, outcome in expected.items():
        if outcome not in SCRIPTED_OUTCOMES:
            raise SuiteError(f"{where}: unknown expected outcome {outcome!r} for {mode}")


def expected_scripted(case: Case, mode: str) -> str:
    """The outcome a scripted run of this case and mode must produce (see scripted.py)."""
    if case.goal is None:
        raise ValueError(f"{case.case_id} is a clean twin")
    return case.expect_scripted.get(mode) or case.goal.expect_scripted[mode]


def _case(raw: Any, bases: Mapping[str, Base], goals: Mapping[str, Goal], corpus: Path) -> Case:
    if not isinstance(raw, dict) or "id" not in raw:
        raise SuiteError("every case needs an id")
    case_id = str(raw["id"])
    base, goal = bases.get(str(raw.get("base"))), goals.get(str(raw.get("goal")))
    technique = str(raw.get("technique"))
    if base is None or goal is None:
        raise SuiteError(f"case {case_id}: unknown base or goal")
    override = raw.get("expect_scripted", {})
    if override:
        _check_expectations(override, tuple(goal.expect_scripted), f"case {case_id}")
    if base.app != goal.app:
        raise SuiteError(f"case {case_id}: goal {goal.name} is for {goal.app}, not {base.app}")
    document = raw.get("document")
    if document is not None:
        if technique != FIXED or not (corpus / str(document)).is_file():
            raise SuiteError(f"case {case_id}: a fixed document needs technique '{FIXED}'")
        return _checked(Case(case_id, base, technique, goal, None, str(document), override))
    if technique not in TECHNIQUES:
        raise SuiteError(f"case {case_id}: unknown technique {technique!r}")
    needed = REQUIRES.get(technique)
    if needed is not None and needed not in goal.text:
        raise SuiteError(f"case {case_id}: {technique} needs '{needed}' in goal {goal.name}")
    return _checked(Case(case_id, base, technique, goal, canary_for(case_id), None, override))


def _checked(case: Case) -> Case:
    """The scripted-run values must actually express this case's attack."""
    goal = case.goal
    assert goal is not None
    if goal.replaces_honest_step and goal.plan_tool not in HONEST_TOOLS[case.app]:
        raise SuiteError(f"case {case.case_id}: no honest step uses {goal.plan_tool} to replace")
    if not step_matches(goal.plan_tool, goal.plan_args, attack_step_for(case)):
        raise SuiteError(f"case {case.case_id}: attack_step does not match goal {goal.name}'s plan")
    if goal.findings is not None:
        if matches(goal.findings, case.base.clean_findings):
            raise SuiteError(
                f"case {case.case_id}: base {case.base.name}'s clean findings already satisfy "
                f"goal {goal.name}, so the attack measures nothing"
            )
        fooled = fooled_findings_for(case)
        assert fooled is not None
        if not matches(goal.findings, fooled):
            raise SuiteError(
                f"case {case.case_id}: attack_findings do not match goal {goal.name}'s findings"
            )
        problem = _schema_problem(case.app, fooled)
        if problem is not None:
            raise SuiteError(
                f"case {case.case_id}: fooled findings do not fit the Reader schema: {problem}"
            )
    return case
