"""Matching an attacker's goal against plans, findings and ledger effects.

Uses the policy engine's own operators (`eq`, `in`, `domain_in`, ...), so there is one matching
language in the repo. A matcher is `{field: {operator: operand}}`; every condition must hold.
"""

from collections.abc import Mapping
from typing import Any

from gawe.core.policy.rules import apply_operator, check_operand

Matcher = Mapping[str, Mapping[str, Any]]


def check_matcher(matcher: Matcher, where: str) -> None:
    """Raise ValueError if `matcher` is not a valid mapping of field -> {operator: operand}."""
    if not isinstance(matcher, Mapping) or not matcher:
        raise ValueError(f"{where}: a matcher needs at least one field")
    for field, conditions in matcher.items():
        if not isinstance(conditions, Mapping) or not conditions:
            raise ValueError(f"{where}.{field}: needs at least one operator")
        for op, operand in conditions.items():
            problem = check_operand(op, operand)
            if problem is not None:
                raise ValueError(f"{where}.{field}: {problem}")


def matches(matcher: Matcher, data: Mapping[str, Any]) -> bool:
    return all(
        apply_operator(op, data.get(field), operand)
        for field, conditions in matcher.items()
        for op, operand in conditions.items()
    )


def step_matches(tool: str, args: Matcher, step: Mapping[str, Any]) -> bool:
    """A planned step (`{"tool": ..., "args": {...}}`) that would carry out the attack."""
    step_args = step.get("args")
    return step.get("tool") == tool and isinstance(step_args, Mapping) and matches(args, step_args)
