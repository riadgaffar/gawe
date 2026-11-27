"""Read-only adapters report independent system observations, never tool replies."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from gawe.core.canonical import canonical_json, sha256_hex


@dataclass(frozen=True)
class ReadbackResult:
    status: Literal["agreement", "disagreement", "unavailable"]
    expected: Mapping[str, Any] = field(default_factory=dict)
    observed: Mapping[str, Any] = field(default_factory=dict)
    output: Mapping[str, Any] | None = None
    code: str = "readback_unavailable"
    # What the target reports about the write that isn't compared with the authorized action,
    # such as the record's revision after it (M26, for an undo record). Empty for most writes.
    facts: Mapping[str, Any] = field(default_factory=dict)


class ReadbackAdapter(Protocol):
    def __call__(
        self, operation_key: str, args: Mapping[str, Any], now: datetime, /
    ) -> ReadbackResult: ...


def compare(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any] | None,
    *,
    output: Mapping[str, Any] | None = None,
    facts: Mapping[str, Any] | None = None,
) -> ReadbackResult:
    if observed is None:
        return ReadbackResult("unavailable", expected=expected)
    matches = dict(expected) == dict(observed)
    return ReadbackResult(
        "agreement" if matches else "disagreement",
        expected,
        observed,
        output,
        "readback_agreed" if matches else "readback_mismatch",
        dict(facts or {}),
    )


def digest_text(value: str) -> str:
    return sha256_hex(value.encode("utf-8"))


def operation_identity(action_digest: str, plan_version: int) -> str:
    """Same textual step/action in a different plan is a different operation."""
    return sha256_hex(
        canonical_json(
            {
                "domain": "gawe.write_operation.v1",
                "action_hash": action_digest,
                "plan_version": plan_version,
            }
        )
    )


def job_identity(
    tenant_id: str,
    run_id: str,
    plan_version: int,
    step_id: str,
    action_hash: str,
    operation_key: str,
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "domain": "gawe.readback.v1",
                "tenant_id": tenant_id,
                "run_id": run_id,
                "plan_version": plan_version,
                "step_id": step_id,
                "action_hash": action_hash,
                "operation_key": operation_key,
            }
        )
    )
