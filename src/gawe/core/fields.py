"""Which argument fields are typed and which are free text the model wrote.

Every manifest declares, for each leaf path of its execution schema, `typed` (ids, amounts,
levels, recipients, record versions, dates) or `free_text` (prose the model wrote, possibly from
an untrusted document). The declaration is explicit and complete, checked at registration: a
constrained-looking string can still carry persuasion, and an unconstrained one (a recipient)
must still be shown. The approval view shows typed fields first and free text collapsed.

A leaf is a property whose schema is not a plain object with declared properties. Values found
in arguments at paths the declaration does not cover are treated as free text.
"""

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

FieldClass = Literal["typed", "free_text"]
FREE_TEXT_LABEL = "Written by the model from an untrusted document"

_INDIRECT = frozenset(
    {"$ref", "$dynamicRef", "allOf", "anyOf", "oneOf", "not", "if", "then", "else"}
)


def schema_leaves(schema: Mapping[str, Any], prefix: str = "") -> list[str]:
    """Leaf paths of an object schema, in declaration order."""
    leaves: list[str] = []
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return leaves
    for name, sub in properties.items():
        path = f"{prefix}{name}"
        if _is_branch(sub):
            leaves.extend(schema_leaves(sub, f"{path}."))
        else:
            leaves.append(path)
    return leaves


def _is_branch(schema: Any) -> bool:
    return (
        isinstance(schema, Mapping)
        and schema.get("type") == "object"
        and isinstance(schema.get("properties"), Mapping)
        and bool(schema["properties"])
        and not _INDIRECT.intersection(schema)
    )


def classification_problem(
    schema: Mapping[str, Any], classes: Mapping[str, str] | None
) -> str | None:
    """A description of what is wrong with a declaration, or None when it is complete."""
    if classes is None:
        return "declares no field classification (typed or free_text per leaf)"
    leaves = schema_leaves(schema)
    undeclared = [leaf for leaf in leaves if leaf not in classes]
    if undeclared:
        return f"undeclared fields: {', '.join(undeclared)}"
    unknown = sorted(set(classes) - set(leaves))
    if unknown:
        return f"classified paths are not fields of the schema: {', '.join(unknown)}"
    bad = sorted(p for p, c in classes.items() if c not in ("typed", "free_text"))
    if bad:
        return f"fields must be typed or free_text: {', '.join(bad)}"
    return None


@dataclass(frozen=True)
class ArgField:
    path: str
    kind: FieldClass
    value: Any


def fields(args: Mapping[str, Any], classes: Mapping[str, str]) -> list[ArgField]:
    """Every value in `args`, classified: declared fields in declaration order, then anything
    the declaration does not cover, as free text."""
    out = [
        ArgField(path, "typed" if kind == "typed" else "free_text", value)
        for path, kind in classes.items()
        if (value := _lookup(args, path)) is not _ABSENT
    ]
    covered = {f.path for f in out}
    out.extend(
        ArgField(path, "free_text", value)
        for path, value in _flatten(args, "", classes)
        if path not in covered
    )
    return out


def length(value: Any) -> int:
    """Unicode code points of the text (non-strings as their JSON form)."""
    return len(value if isinstance(value, str) else _json(value))


def digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


_ABSENT = object()


def _lookup(args: Mapping[str, Any], path: str) -> Any:
    current: Any = args
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _ABSENT
        current = current[part]
    return current


def _flatten(
    value: Mapping[str, Any], prefix: str, classes: Mapping[str, str]
) -> Iterator[tuple[str, Any]]:
    for key, item in value.items():
        path = f"{prefix}{key}"
        if path in classes:
            continue
        if isinstance(item, Mapping) and item and any(c.startswith(f"{path}.") for c in classes):
            yield from _flatten(item, f"{path}.", classes)
        else:
            yield path, item


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class Change:
    path: str
    kind: FieldClass
    change: Literal["added", "removed", "changed", "unchanged"]
    before: Any = None
    after: Any = None


def difference(
    current: Mapping[str, Any],
    current_classes: Mapping[str, str],
    previous: Mapping[str, Any],
    previous_classes: Mapping[str, str],
) -> list[Change]:
    """Typed fields as added, removed or changed (with values); free text only as changed or
    unchanged, by digest, never with content. A field typed on only one side counts as free
    text, so a value is never shown from a side where it was free text."""
    now = {f.path: f for f in fields(current, current_classes)}
    before = {f.path: f for f in fields(previous, previous_classes)}
    changes: list[Change] = []
    for path in [*now, *(p for p in before if p not in now)]:
        a, b = before.get(path), now.get(path)
        typed = (a is None or a.kind == "typed") and (b is None or b.kind == "typed")
        if typed:
            if a is None and b is not None:
                changes.append(Change(path, "typed", "added", after=b.value))
            elif b is None and a is not None:
                changes.append(Change(path, "typed", "removed", before=a.value))
            elif a is not None and b is not None and _json(a.value) != _json(b.value):
                changes.append(Change(path, "typed", "changed", before=a.value, after=b.value))
        else:
            same = a is not None and b is not None and digest(a.value) == digest(b.value)
            changes.append(Change(path, "free_text", "unchanged" if same else "changed"))
    return changes
