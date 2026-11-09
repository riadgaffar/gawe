"""Canonical JSON and action hashing.

The action hash is what an approval binds to. It must be deterministic across processes and
key orderings, and must change if anything that affects the side effect changes.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

_ACTION_DOMAIN = b"gawe.action.v1\n"


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented in canonical JSON."""


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("NaN and infinity are not allowed")
        # 1.0 and 1 must hash identically.
        return int(value) if value.is_integer() else value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"object keys must be strings, got {type(key).__name__}"
                )
            out[key] = _normalize(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    raise CanonicalizationError(f"unsupported type: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Deterministic UTF-8 JSON: sorted keys, no whitespace, no NaN/inf."""
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def action_hash(
    *,
    tool: str,
    version: str,
    resolved_args: Mapping[str, Any],
    run_id: str,
    step_id: str,
) -> str:
    """Hash of the exact action: tool, version, fully resolved args, and its place in a run."""
    payload = {
        "tool": tool,
        "version": version,
        "args": resolved_args,
        "run_id": run_id,
        "step_id": step_id,
    }
    return sha256_hex(_ACTION_DOMAIN + canonical_json(payload))
