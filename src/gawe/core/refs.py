"""Resolving `{"$ref": "<step_id>.output.<path>"}` arguments from earlier steps' outputs.

The plan validator already guarantees that references are whole top-level argument values that
name a declared dependency. This module does the run-time half: look the value up in that step's
saved output. A missing step, output or path is an error; nothing is guessed or defaulted.
"""

from collections.abc import Mapping
from typing import Any

from gawe.core.models import REF_KEY, is_ref


class RefResolutionError(Exception):
    """A reference could not be resolved. The message never includes output values."""


def _lookup(output: Mapping[str, Any], path: list[str], ref: str) -> Any:
    current: Any = output
    for part in path:
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise RefResolutionError(f"reference {ref} does not exist in the step's output")
    return current


def resolve_args(
    template: Mapping[str, Any], outputs: Mapping[str, Mapping[str, Any] | None]
) -> dict[str, Any]:
    """Return `template` with every top-level reference replaced by the referenced value."""
    resolved: dict[str, Any] = {}
    for key, value in template.items():
        if not is_ref(value):
            resolved[key] = value
            continue
        ref = value[REF_KEY]
        step_id, _, rest = ref.partition(".output")
        output = outputs.get(step_id)
        if output is None:
            raise RefResolutionError(f"reference {ref} points at a step with no saved output")
        path = [part for part in rest.split(".") if part]
        resolved[key] = _lookup(output, path, ref)
    return resolved
