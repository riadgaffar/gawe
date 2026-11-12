"""Validate trusted app declarations, not arbitrary callback/connector Python data flow.

Resource names describe application state, not engine-owned audit/idempotency bookkeeping.
All leaf kinds participate in overlap checks; calling a source configuration grants no exemption.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Literal

from gawe.core.models import FrozenModel, ToolManifest
from gawe.core.policy.rules import Rule, RuleConfigError

_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")


class PolicySource(FrozenModel):
    kind: Literal["configuration", "findings", "resource"]
    resource: str


class PolicyVariable(FrozenModel):
    dependencies: tuple[str, ...]


def _name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise RuleConfigError(f"invalid provenance name {name!r}; use canonical dotted names")


def _overlaps(left: str, right: str) -> bool:
    return left == right or left.startswith(right + ".") or right.startswith(left + ".")


def validate_provenance(
    rules: Sequence[Rule],
    manifests: Sequence[ToolManifest],
    sources: Mapping[str, PolicySource],
    variables: Mapping[str, PolicyVariable],
) -> None:
    """Fail startup for incomplete provenance or any transitive tool-write dependency."""
    writes: list[tuple[str, str]] = []
    for manifest in manifests:
        identity = f"{manifest.name}@{manifest.version}"
        if manifest.writes is None:
            raise RuleConfigError(f"{identity}: missing write-resource declaration")
        if manifest.side_effect_class == "read" and manifest.writes:
            raise RuleConfigError(f"{identity}: read tool declares writes")
        for resource in manifest.writes:
            _name(resource)
            writes.append((resource, identity))

    for name, source in sources.items():
        _name(name)
        _name(source.resource)
    for name in variables:
        _name(name)
    if sources.keys() & variables.keys():
        raise RuleConfigError("source and variable names must be disjoint")

    resolved: dict[str, frozenset[str]] = {}
    visiting: set[str] = set()

    def resolve(name: str) -> frozenset[str]:
        _name(name)
        if name in sources:
            return frozenset({sources[name].resource})
        if name in resolved:
            return resolved[name]
        if name in visiting:
            raise RuleConfigError(f"cyclic policy provenance at '{name}'")
        if name not in variables:
            raise RuleConfigError(f"unknown provenance dependency '{name}'")
        dependencies = variables[name].dependencies
        if not dependencies:
            raise RuleConfigError(f"variable '{name}' has empty provenance")
        visiting.add(name)
        resources = frozenset(resource for dep in dependencies for resource in resolve(dep))
        visiting.remove(name)
        resolved[name] = resources
        return resources

    # Include unused definitions: a future YAML reference must not expose an unsafe variable.
    for name in variables:
        for resource in sorted(resolve(name)):
            for written, identity in writes:
                if _overlaps(resource, written):
                    raise RuleConfigError(
                        f"variable '{name}' source '{resource}' overlaps "
                        f"{identity} write '{written}'"
                    )
    for rule in rules:
        for conditions in rule.when.args.values():
            for operand in conditions.values():
                if isinstance(operand, str) and operand.startswith("$"):
                    name = operand[1:]
                    if name not in variables:
                        raise RuleConfigError(
                            f"rule '{rule.id}': undeclared policy variable '{operand}'"
                        )
