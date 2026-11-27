"""What a target system guarantees for a write, and what each write needs from it (M22).

A connector declares, per tool version, what its system enforces. Startup derives what each write
relies on and refuses to start when the target doesn't guarantee it:

* `idempotent_by_key`: the system records an operation once per GAWE's key, so a retry after a
  lost reply can't apply it twice. Every write needs it.
* `authoritative_readback`: the system's own record of the operation can be read by GAWE's key,
  so its outcome is confirmed (M16a). Searchable labels or delayed history don't count: absence
  there doesn't prove an operation failed. Every write needs it.
* `conditional_write`: the system checks a bound record revision in the write's own transaction
  (M16b). A write that binds a record needs it.
* `atomic_reservation`: the system reserves against a running total in the write's own
  transaction, so two writes can't both pass a cap that only one fits. A write that a rule judges
  with a running total (a `counter` policy source) needs it; separate numeric checks race.
* `reversible` (M26): the system can put back the state a write replaced, only while the record
  still has the revision it had right after that write, checked in the reversal's own
  transaction, and it reports that revision on read-back. No write needs it; an app may make a
  write undoable only when its target declares it.

A tool version the app makes prepare-only (M20) needs nothing: GAWE never sends it.

A condition in another system (an IT ticket still open, for a grant) can't be enforced by the
target at all. It is declared as a `DispatchCondition`: checked just before sending, not enforced.
"""

import fnmatch
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, get_args

from gawe.core.models import ToolManifest
from gawe.core.policy.provenance import PolicySource, PolicyVariable
from gawe.core.policy.rules import Rule
from gawe.core.records import StepRecord
from gawe.core.runs import RunRecord

Guarantee = Literal[
    "idempotent_by_key",
    "authoritative_readback",
    "conditional_write",
    "atomic_reservation",
    "reversible",
]
GUARANTEES: frozenset[str] = frozenset(get_args(Guarantee))
EVERY_WRITE: frozenset[Guarantee] = frozenset({"idempotent_by_key", "authoritative_readback"})


def rule_selects(rule: Rule, manifest: ToolManifest) -> bool:
    """Whether a rule can apply to this tool version, by its tool, class and tier selectors."""
    when = rule.when
    return (
        (when.tool is None or fnmatch.fnmatchcase(manifest.name, when.tool))
        and (when.side_effect_class is None or manifest.side_effect_class == when.side_effect_class)
        and (when.risk_tier is None or manifest.risk_tier == when.risk_tier)
    )


def counter_variables(
    sources: Mapping[str, PolicySource], variables: Mapping[str, PolicyVariable]
) -> frozenset[str]:
    """Variables that depend, directly or through other variables, on a running total."""
    found: dict[str, bool] = {}

    def counts(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in sources:
            return sources[name].kind == "counter"
        if name in found:
            return found[name]
        if name in seen or name not in variables:
            return False  # cycles and unknown names are reported by the provenance check
        found[name] = any(counts(dep, seen | {name}) for dep in variables[name].dependencies)
        return found[name]

    return frozenset(name for name in variables if counts(name))


def _reads(rule: Rule) -> set[str]:
    return {
        operand[1:]
        for conditions in rule.when.args.values()
        for operand in conditions.values()
        if isinstance(operand, str) and operand.startswith("$")
    }


def required(
    manifest: ToolManifest,
    rules: Sequence[Rule],
    sources: Mapping[str, PolicySource],
    variables: Mapping[str, PolicyVariable],
) -> frozenset[Guarantee]:
    """What a current write relies on its target for. Reads and retired versions need nothing."""
    if manifest.side_effect_class == "read" or manifest.retired:
        return frozenset()
    needs: set[Guarantee] = set(EVERY_WRITE)
    if manifest.record_binding is not None:
        needs.add("conditional_write")
    counters = counter_variables(sources, variables)
    if any(rule_selects(rule, manifest) and _reads(rule) & counters for rule in rules):
        needs.add("atomic_reservation")
    return frozenset(needs)


@dataclass(frozen=True)
class DispatchCondition:
    """A condition a write relies on that its target can't check in the write's own transaction,
    because it lives in another system (M22). The executor reads it last, just before sending,
    and sends nothing if it no longer holds. The window between that read and the write remains;
    it is named, not closed.

    `check` reads the authoritative system and says whether the condition still holds. Raising
    means it couldn't be read, which also stops the write."""

    name: str
    check: Callable[[RunRecord, StepRecord], bool]
