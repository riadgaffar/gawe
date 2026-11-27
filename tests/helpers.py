"""Shared test builders (kept out of conftest so they can be imported explicitly)."""

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any

from gawe.core.fields import schema_leaves
from gawe.core.models import PolicyDecision, ToolManifest
from gawe.core.policy.rules import EvaluationContext
from gawe.core.policy.signing import PolicySigner

KEY = bytes(range(32))
# A rule-set digest for decisions sealed directly in tests, without a RuleEngine.
DIGEST = "d" * 64
# Format 1 decisions signed with KEY by the code before M16b. Never regenerate these.
GOLDEN_V1 = [
    '{"step_id":"send","action_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"outcome":"needs_approval","rule_ids":["esign-needs-legal"],'
    '"reason":"Signature requests need legal approval","approver_role":"legal",'
    '"signature":"4ca639dcab009a3c7f82417da3f2a6b5a47ea747fbf390509f17b87f699c3150"}',
    '{"step_id":"read","action_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    '"outcome":"allow","rule_ids":["read-allowed"],"reason":"allow by read-allowed",'
    '"approver_role":null,'
    '"signature":"e2ef203039fa8325f53633cbc54aa35f712272fc36ec9debc5eafc5947f02944"}',
]
DEFAULT_VARIABLES: dict[str, Any] = {"tenant": {"internal_domains": ["acme.com"]}}


def signer() -> PolicySigner:
    return PolicySigner(KEY)


def classify(schema: Mapping[str, Any], free_text: Sequence[str] = ()) -> dict[str, Any]:
    """A complete field classification for a test schema: free_text as named, the rest typed."""
    return {leaf: "free_text" if leaf in free_text else "typed" for leaf in schema_leaves(schema)}


def tool(
    name: str,
    side_effect: str,
    risk: str,
    properties: dict[str, Any],
    required: Sequence[str] = (),
    roles: Sequence[str] = (),
    free_text: Sequence[str] = (),
) -> ToolManifest:
    input_schema = {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }
    return ToolManifest(
        name=name,
        version="1.0.0",
        input_schema=input_schema,
        field_classes=classify(input_schema, free_text),
        output_schema={"type": "object"},
        side_effect_class=side_effect,  # type: ignore[arg-type]
        risk_tier=risk,  # type: ignore[arg-type]
        required_roles=tuple(roles),
        writes=() if side_effect == "read" else (name,),
    )


TOOLS: dict[str, ToolManifest] = {
    "contracts.extract_risks": tool(
        "contracts.extract_risks",
        "read",
        "low",
        {"document_id": {"type": "string"}},
        ["document_id"],
    ),
    "contracts.send_email": tool(
        "contracts.send_email",
        "external",
        "high",
        {"to": {"type": "string"}, "body": {"type": "string"}},
        ["to", "body"],
        roles=["case_manager", "legal_reviewer"],
        free_text=["body"],
    ),
    "crm.write_assessment": tool(
        "crm.write_assessment",
        "write",
        "medium",
        {"contract_id": {"type": "string"}, "risk_score": {"type": "integer"}},
        ["contract_id", "risk_score"],
    ),
    "crm.mark_approved_for_signature": tool(
        "crm.mark_approved_for_signature",
        "write",
        "high",
        {"contract_id": {"type": "string"}, "deal_value": {"type": "number", "minimum": 0}},
        ["contract_id", "deal_value"],
        roles=["case_manager"],
    ),
    "esign.send_for_signature": tool(
        "esign.send_for_signature",
        "external",
        "high",
        {"contract_id": {"type": "string"}},
        ["contract_id"],
    ),
}


def make_ctx(
    manifest: ToolManifest,
    args: Mapping[str, Any],
    *,
    roles: Sequence[str] = (),
    variables: Mapping[str, Any] | None = None,
    run_id: str = "run1",
    step_id: str = "s1",
    user: str = "u1",
) -> EvaluationContext:
    return EvaluationContext(
        run_id=run_id,
        step_id=step_id,
        manifest=manifest,
        resolved_args=dict(args),
        acting_user_id=user,
        user_roles=frozenset(roles),
        variables=DEFAULT_VARIABLES if variables is None else variables,
    )


def current_manifest(manifests: Sequence[ToolManifest], name: str) -> ToolManifest:
    """The executable (not retired) version of a tool."""
    return max(
        (m for m in manifests if m.name == name and not m.retired),
        key=lambda m: tuple(int(v) for v in m.version.split(".")),
    )


TEST_REVISION = "rev-test"
TEST_EXPIRY = "2026-11-22T00:00:00Z"
TEST_PRIOR = {"risk_level": "none", "summary": ""}


def bound_args(manifest: ToolManifest, args: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit direct-policy fixture: the trusted fields preparation adds for a bound write.

    Fills only fields the arguments lack, so a test can still set or mutate any of them."""
    trusted: dict[str, Any] = {}
    if manifest.record_binding == "clm.contract":
        trusted = {"record": {"contract_version": TEST_REVISION}}
    elif manifest.record_binding == "iam.user":
        trusted = {
            "record": {"user_version": TEST_REVISION},
            "expires_at": TEST_EXPIRY,
            "lifetime_hours": 168,
        }
    if "prior" in manifest.input_schema.get("properties", {}):  # M26: the rating it replaces
        trusted["prior"] = TEST_PRIOR
    if "snapshots" in manifest.input_schema.get("properties", {}) and "snapshots" not in args:
        # Direct-policy fixture only: explicitly supply the new required trusted envelope.
        from gawe.core.record_changes import CapturedField, RecordSnapshot

        resource = manifest.record_binding
        values = {"deal_value": args.get("deal_value", 45000)} if resource == "clm.contract" else {}
        records = args.get(
            "batch", [{"subject": args.get("contract_id", args.get("email", "fixture"))}]
        )
        trusted["snapshots"] = [
            RecordSnapshot(
                resource=resource,
                subject=row.get("event_id", row.get("subject", "fixture")),
                revision=row.get("revision", TEST_REVISION),
                captured_at="2026-11-25T00:00:00Z",
                fields=tuple(
                    CapturedField(path=k, value=v)
                    for k, v in (
                        {"customer": row["customer"], "amount": row["amount"]}
                        if resource == "billing.event"
                        else values
                    ).items()
                ),
            ).model_dump(mode="json")
            for row in records
        ]
    return {**trusted, **args}


def prepared_email(args: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit direct-policy fixture, not a directory result or expected-outcome oracle."""
    return {
        **args,
        "delivery": {
            "mailboxes": [args["to"]],
            "directory_marker": "fixture:1",
            "reapproval_required": False,
        },
    }


def seal_v1(
    *,
    step_id: str,
    action_hash: str,
    outcome: str,
    rule_ids: Sequence[str],
    reason: str,
    approver_role: str | None = None,
    key: bytes = KEY,
) -> PolicyDecision:
    """A format 1 decision exactly as GAWE issued them before M16b.

    Written out here, not borrowed from the signer, so it stays a fixed reference; the golden
    fixtures in test_decision_format.py pin it to signatures recorded before the change.
    """
    body = json.dumps(
        {
            "step_id": step_id,
            "action_hash": action_hash,
            "outcome": outcome,
            "rule_ids": list(rule_ids),
            "reason": reason,
            "approver_role": approver_role,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    signature = hmac.new(key, b"gawe.decision.v1\n" + body, hashlib.sha256).hexdigest()
    return PolicyDecision.model_validate(
        {
            "step_id": step_id,
            "action_hash": action_hash,
            "outcome": outcome,
            "rule_ids": list(rule_ids),
            "reason": reason,
            "approver_role": approver_role,
            "signature": signature,
        }
    )
