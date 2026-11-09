"""Policy rules: loading, validation and matching.

Rules are declarative YAML. Matching is pure and deterministic; nothing here calls an LLM.

Semantics worth knowing before writing a rule:

* A rule matches when *all* of its `when` predicates hold.
* Missing or ill-typed values make positive operators (eq, in, gt, ...) false and negative
  operators (ne, not_in, domain_not_in) true, so uncertainty never widens what is allowed.
* An operand written as "$a.b" is resolved from the evaluation context's variables (for example
  "$tenant.internal_domains"). An unresolvable variable is an evaluation error, which the engine
  turns into a deny.
"""

import fnmatch
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import Field, ValidationError, model_validator

from gawe.core.models import (
    FrozenModel,
    PolicyOutcome,
    RiskTier,
    SideEffectClass,
    ToolManifest,
)

RULE_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"
# Rule ids the engine emits itself; YAML rules may not claim them.
RESERVED_ID_PREFIXES: Final = ("rbac.", "engine.")
RESERVED_IDS: Final = frozenset({"default-deny"})

MISSING: Final = object()


class RuleConfigError(ValueError):
    """The policy file is malformed. Raised at load time, never during evaluation."""


class PolicyEvaluationError(Exception):
    """A rule could not be evaluated (for example an unresolvable variable). Engine denies."""


@dataclass(frozen=True)
class EvaluationContext:
    run_id: str
    step_id: str
    manifest: ToolManifest
    resolved_args: Mapping[str, Any]
    acting_user_id: str
    user_roles: frozenset[str]
    variables: Mapping[str, Any] = field(default_factory=dict)


# --- operators ---------------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s,;<>()\[\]\"']+@([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)$")


def email_domain(value: Any) -> str | None:
    """Domain of a *single plain* address, else None.

    Lists, display names ("Bob <b@x.com>") and comma/semicolon-joined strings are rejected on
    purpose: "attacker@evil.com, a@internal.com" must never be judged by its last address.
    """
    if not isinstance(value, str):
        return None
    match = _EMAIL_RE.match(value)
    return match.group(1).lower() if match else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same(value: Any, operand: Any) -> bool:
    # bool is an int subclass; keep True distinct from 1.
    return isinstance(value, bool) == isinstance(operand, bool) and bool(value == operand)


def _op_eq(value: Any, operand: Any) -> bool:
    return value is not MISSING and _same(value, operand)


def _op_ne(value: Any, operand: Any) -> bool:
    return value is MISSING or not _same(value, operand)


def _op_in(value: Any, operand: Any) -> bool:
    return value is not MISSING and any(_same(value, item) for item in operand)


def _op_not_in(value: Any, operand: Any) -> bool:
    return not _op_in(value, operand)


def _compare(check: Callable[[float, float], bool]) -> Callable[[Any, Any], bool]:
    def op(value: Any, operand: Any) -> bool:
        return _is_number(value) and bool(check(value, operand))

    return op


def _op_exists(value: Any, operand: Any) -> bool:
    return bool((value is not MISSING) == operand)


def _op_domain_in(value: Any, operand: Any) -> bool:
    domain = email_domain(value)
    return domain is not None and domain in {str(item).lower() for item in operand}


def _op_domain_not_in(value: Any, operand: Any) -> bool:
    return not _op_domain_in(value, operand)


_OPS: Final[dict[str, Callable[[Any, Any], bool]]] = {
    "eq": _op_eq,
    "ne": _op_ne,
    "in": _op_in,
    "not_in": _op_not_in,
    "gt": _compare(lambda v, o: v > o),
    "gte": _compare(lambda v, o: v >= o),
    "lt": _compare(lambda v, o: v < o),
    "lte": _compare(lambda v, o: v <= o),
    "exists": _op_exists,
    "domain_in": _op_domain_in,
    "domain_not_in": _op_domain_not_in,
}


def apply_operator(op: str, value: Any, operand: Any) -> bool:
    """Apply one rule operator to a value, exactly as policy does (for tools that match like it)."""
    problem = check_operand(op, operand)
    if problem is not None:
        raise ValueError(problem)
    return _OPS[op](value, operand)


def check_operand(op: str, operand: Any) -> str | None:
    """Return a problem description if `operand` is unusable for `op`, else None."""
    if op not in _OPS:
        return f"unknown operator '{op}'"
    if op in {"gt", "gte", "lt", "lte"} and not _is_number(operand):
        return f"'{op}' needs a number"
    if op == "exists" and not isinstance(operand, bool):
        return "'exists' needs true or false"
    if op in {"in", "not_in"} and not isinstance(operand, (list, tuple)):
        return f"'{op}' needs a list"
    if op in {"domain_in", "domain_not_in"} and (
        not isinstance(operand, (list, tuple)) or not all(isinstance(i, str) for i in operand)
    ):
        return f"'{op}' needs a list of domain strings"
    if op in {"eq", "ne"} and isinstance(operand, (dict, list, tuple)):
        return f"'{op}' needs a scalar"
    return None


def _is_variable(operand: Any) -> bool:
    return isinstance(operand, str) and operand.startswith("$")


# --- rule model --------------------------------------------------------------------------------


class When(FrozenModel):
    tool: str | None = None  # exact name or glob, e.g. "contracts.*"
    side_effect_class: SideEffectClass | None = None
    risk_tier: RiskTier | None = None
    # dotted arg path -> {operator: operand}
    args: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "When":
        if (
            self.tool is None
            and self.side_effect_class is None
            and self.risk_tier is None
            and not self.args
        ):
            raise ValueError("'when' must have at least one predicate (use tool: '*' to match all)")
        for path, conditions in self.args.items():
            if not conditions:
                raise ValueError(f"args.{path} has no operators")
            for op, operand in conditions.items():
                if _is_variable(operand):
                    if op not in _OPS:
                        raise ValueError(f"unknown operator '{op}'")
                    continue
                problem = check_operand(op, operand)
                if problem:
                    raise ValueError(f"args.{path}: {problem}")
        return self


class Rule(FrozenModel):
    id: str = Field(pattern=RULE_ID_PATTERN)
    effect: PolicyOutcome
    when: When
    approver_role: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Rule":
        if self.id in RESERVED_IDS or self.id.startswith(RESERVED_ID_PREFIXES):
            raise ValueError(f"rule id '{self.id}' is reserved for the engine")
        if self.effect == "needs_approval" and not self.approver_role:
            raise ValueError("needs_approval rules must set approver_role")
        if self.effect != "needs_approval" and self.approver_role is not None:
            raise ValueError("approver_role is only valid on needs_approval rules")
        return self


# --- loading -----------------------------------------------------------------------------------


def parse_rules(text: str) -> tuple[Rule, ...]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleConfigError(f"policy is not valid YAML: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"rules"}:
        raise RuleConfigError("policy must be a mapping with exactly one key: 'rules'")
    raw_rules = document["rules"]
    if not isinstance(raw_rules, list):
        raise RuleConfigError("'rules' must be a list")

    rules: list[Rule] = []
    for index, raw in enumerate(raw_rules):
        try:
            rules.append(Rule.model_validate(raw))
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '<rule>'}: {err['msg']}"
                for err in exc.errors()
            )
            raise RuleConfigError(f"rule #{index + 1}: {details}") from exc

    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise RuleConfigError(f"duplicate rule id '{rule.id}'")
        seen.add(rule.id)
    return tuple(rules)


def load_rules(path: str | Path) -> tuple[Rule, ...]:
    return parse_rules(Path(path).read_text(encoding="utf-8"))


# --- matching ----------------------------------------------------------------------------------


def lookup(data: Mapping[str, Any], dotted_path: str) -> Any:
    """Value at a dotted path, or MISSING."""
    current: Any = data
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return MISSING
        current = current[part]
    return current


def _resolve(operand: Any, variables: Mapping[str, Any]) -> Any:
    if not _is_variable(operand):
        return operand
    name = operand[1:]
    resolved = lookup(variables, name)
    if resolved is MISSING:
        raise PolicyEvaluationError(f"unresolved policy variable '{operand}'")
    return resolved


def rule_matches(rule: Rule, ctx: EvaluationContext) -> bool:
    when = rule.when
    manifest = ctx.manifest
    if when.tool is not None and not fnmatch.fnmatchcase(manifest.name, when.tool):
        return False
    if when.side_effect_class is not None and manifest.side_effect_class != when.side_effect_class:
        return False
    if when.risk_tier is not None and manifest.risk_tier != when.risk_tier:
        return False
    for path, conditions in when.args.items():
        value = lookup(ctx.resolved_args, path)
        for op, operand in conditions.items():
            resolved = _resolve(operand, ctx.variables)
            problem = check_operand(op, resolved)
            if problem:
                raise PolicyEvaluationError(f"rule '{rule.id}' arg '{path}': {problem}")
            if not _OPS[op](value, resolved):
                return False
    return True
