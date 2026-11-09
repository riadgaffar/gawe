"""Plan validation. The plan is untrusted LLM output: nothing here trusts its shape or content.

Validation is structural and static. It says nothing about whether a step is *allowed*; that is
the policy engine's job, evaluated per step at execution time.
"""

import re
from collections import deque
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from gawe.core.models import REF_KEY, Plan, PlanStep, ToolManifest, has_ref_key, is_ref
from gawe.core.registry import ToolNotFoundError, ToolRegistry

DEFAULT_MAX_STEPS = 25
_REF_RE = re.compile(r"^([a-z][a-z0-9_]{0,63})\.output(\.[A-Za-z0-9_\-]+)*$")
_MAX_MESSAGE = 200


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    step_id: str | None = None


class PlanValidationError(Exception):
    def __init__(self, issues: tuple[Issue, ...]) -> None:
        super().__init__("; ".join(f"{i.code}: {i.message}" for i in issues))
        self.issues = issues


@dataclass(frozen=True)
class ValidationResult:
    plan: Plan | None
    issues: tuple[Issue, ...]

    @property
    def ok(self) -> bool:
        return self.plan is not None and not self.issues

    def raise_if_invalid(self) -> Plan:
        if self.plan is None or self.issues:
            raise PlanValidationError(self.issues)
        return self.plan


def _short(text: str) -> str:
    return text if len(text) <= _MAX_MESSAGE else text[: _MAX_MESSAGE - 3] + "..."


def _ancestors(steps: Sequence[PlanStep]) -> dict[str, frozenset[str]] | None:
    """Transitive dependencies per step via Kahn's algorithm; None if the graph has a cycle.
    Callers must ensure ids are unique and every dependency exists."""
    deps = {s.id: set(s.depends_on) for s in steps}
    children: dict[str, list[str]] = {sid: [] for sid in deps}
    for sid, parents in deps.items():
        for parent in parents:
            children[parent].append(sid)
    remaining = {sid: len(parents) for sid, parents in deps.items()}
    ready = deque(sid for sid, n in remaining.items() if n == 0)
    ancestors: dict[str, set[str]] = {sid: set() for sid in deps}
    visited = 0
    while ready:
        sid = ready.popleft()
        visited += 1
        for child in children[sid]:
            ancestors[child] |= ancestors[sid] | {sid}
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
    if visited != len(deps):
        return None
    return {sid: frozenset(found) for sid, found in ancestors.items()}


def _check_args(
    step: PlanStep,
    manifest: ToolManifest,
    ancestors: frozenset[str] | None,
) -> list[Issue]:
    issues: list[Issue] = []
    literals: dict[str, Any] = {}
    ref_keys: set[str] = set()
    properties = manifest.input_schema.get("properties")

    for key, value in step.args.items():
        if is_ref(value):
            ref_keys.add(key)
            match = _REF_RE.match(value[REF_KEY])
            if match is None:
                issues.append(Issue("BAD_REF", f"arg '{key}' has a malformed reference", step.id))
            elif match.group(1) not in step.depends_on or (
                ancestors is not None and match.group(1) not in ancestors
            ):
                issues.append(
                    Issue(
                        "REF_NOT_DEPENDENCY",
                        f"arg '{key}' references '{match.group(1)}', which is not a declared "
                        "dependency of this step",
                        step.id,
                    )
                )
            if isinstance(properties, dict) and key not in properties:
                issues.append(
                    Issue("UNKNOWN_ARG", f"arg '{key}' is not in the tool schema", step.id)
                )
        elif has_ref_key(value):
            issues.append(
                Issue(
                    "MALFORMED_REF",
                    f"arg '{key}' contains a reference that is nested or malformed; "
                    "references must be the entire value of a top-level arg",
                    step.id,
                )
            )
        else:
            literals[key] = value

    # Referenced values are unknown until runtime, so relax `required` for them and validate
    # the literal remainder. Full validation of resolved args happens again before policy/approval.
    schema = deepcopy(manifest.input_schema)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [r for r in required if r not in ref_keys]
    validator = Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(literals), key=lambda e: list(map(str, e.path))):
        where = ".".join(str(p) for p in error.absolute_path) or "<args>"
        issues.append(Issue("ARG_SCHEMA", _short(f"{where}: {error.message}"), step.id))
    return issues


def validate_plan(
    plan: Plan,
    registry: ToolRegistry,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> ValidationResult:
    issues: list[Issue] = []
    steps = plan.steps

    if not steps:
        return ValidationResult(plan, (Issue("EMPTY_PLAN", "plan has no steps"),))
    if len(steps) > max_steps:
        issues.append(
            Issue("TOO_MANY_STEPS", f"{len(steps)} steps exceeds the limit of {max_steps}")
        )

    ids = [s.id for s in steps]
    structural_ok = len(set(ids)) == len(ids)
    if not structural_ok:
        issues.append(Issue("DUPLICATE_STEP_ID", "step ids must be unique"))
    known = set(ids)

    manifests: dict[str, ToolManifest] = {}
    for step in steps:
        try:
            manifests[step.id] = registry.get(step.tool)
        except ToolNotFoundError:
            issues.append(Issue("UNKNOWN_TOOL", f"tool '{step.tool}' is not registered", step.id))
        for dep in step.depends_on:
            if dep == step.id:
                issues.append(Issue("SELF_DEPENDENCY", "step depends on itself", step.id))
                structural_ok = False
            elif dep not in known:
                issues.append(Issue("UNKNOWN_DEPENDENCY", f"unknown dependency '{dep}'", step.id))
                structural_ok = False

    ancestors: dict[str, frozenset[str]] | None = None
    if structural_ok:
        ancestors = _ancestors(steps)
        if ancestors is None:
            issues.append(Issue("CYCLE", "step dependencies contain a cycle"))

    for step in steps:
        manifest = manifests.get(step.id)
        if manifest is not None:
            step_ancestors = ancestors.get(step.id) if ancestors is not None else None
            issues.extend(_check_args(step, manifest, step_ancestors))

    return ValidationResult(plan, tuple(issues))


def validate_raw_plan(
    raw: object,
    registry: ToolRegistry,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> ValidationResult:
    """Parse an untrusted plan (JSON string/bytes or already-decoded object), then validate it."""
    try:
        if isinstance(raw, (str, bytes)):
            plan = Plan.model_validate_json(raw)
        else:
            plan = Plan.model_validate(raw)
    except ValidationError as exc:
        issues = tuple(
            Issue(
                "SCHEMA",
                _short(f"{'.'.join(str(p) for p in err['loc']) or '<plan>'}: {err['msg']}"),
            )
            for err in exc.errors()
        )
        return ValidationResult(None, issues)
    return validate_plan(plan, registry, max_steps=max_steps)
