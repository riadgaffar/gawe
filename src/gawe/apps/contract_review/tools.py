"""The four Contract Review tools: their manifests, implementations and post-conditions.

Policy decides what may run; these only describe and perform the work. Two of them defend
themselves as well:

* `esign.send_for_signature` takes `deal_value` as an argument because the approval rules need to
  see it, but the model supplies arguments, so the write checks the value against the contract
  system and refuses on a mismatch. A planner that understates the value to dodge the finance
  approval gets a refused call, not a signature request.
* Since M26 the assessment write is 3.0.0: it sets the contract's current rating and moves its
  revision, and trusted preparation adds the rating it replaces (`prior`) inside the hash, so an
  undo record knows exactly what to restore. 2.0.0 stays registered (retired) to verify.
* The two contract writes (version 2.0.0) carry the contract's revision, added by trusted
  preparation before policy and hashing. The contract system accepts the write only while the
  contract still has that revision, checked in the write's own transaction, so an approval never
  applies to a contract that was changed after it was read. Version 1.0.0 of both stays
  registered (retired) only to verify writes dispatched before this existed.
* Write replies satisfy an output protocol; version-specific read-back adapters independently
  compare the target system's original operation record with the authorized action. Echoes are
  not proof of a write. Read-tool verification remains unchanged.
"""

from collections.abc import Mapping
from typing import Any

from gawe.apps.record_binding import bound, with_snapshots
from gawe.connectors.credentials import Credential, ScopedClient, ScopedSystem
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_email import MockEmail
from gawe.connectors.mock_esign import MockESign
from gawe.core.executor import ToolCall, ToolImplementation
from gawe.core.fields import FieldClass
from gawe.core.models import ToolManifest
from gawe.core.verifier import PostCondition

CONTRACT_ID = {"type": "string", "pattern": r"^C-\d{4}$"}
EMAIL = {"type": "string", "maxLength": 254}

# Approval-card order: recipients, amounts, ids, levels, record versions. Summaries, subjects and
# bodies are written by the model, possibly from an untrusted contract, so they are free text.
ASSESSMENT_FIELDS: dict[str, FieldClass] = {
    "contract_id": "typed",
    "risk_level": "typed",
    "summary": "free_text",
}
SIGNATURE_FIELDS: dict[str, FieldClass] = {
    "signer_email": "typed",
    "deal_value": "typed",
    "contract_id": "typed",
}
BOUND_VERSION: dict[str, FieldClass] = {"record.contract_version": "typed"}
# M26: the rating a 3.0.0 write replaces, added by trusted preparation from the same read as the
# bound revision. The level is typed; the summary was written by a model in an earlier run.
PRIOR_FIELDS: dict[str, FieldClass] = {"prior.risk_level": "typed", "prior.summary": "free_text"}
NO_RATING = "none"  # prior.risk_level when the contract had no rating
PRIOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "risk_level": {"enum": [NO_RATING, "low", "medium", "high"]},
        "summary": {"type": "string", "maxLength": 500},
    },
    "required": ["risk_level", "summary"],
}


def with_prior(schema: dict[str, Any]) -> dict[str, Any]:
    """An execution schema plus the trusted `prior` preparation adds."""
    return {
        **schema,
        "properties": {**schema["properties"], "prior": PRIOR_SCHEMA},
        "required": [*schema["required"], "prior"],
    }


ASSESSMENT_PROPOSAL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "contract_id": CONTRACT_ID,
        "risk_level": {"enum": ["low", "medium", "high"]},
        "summary": {"type": "string", "maxLength": 500},
    },
    "required": ["contract_id", "risk_level", "summary"],
    "additionalProperties": False,
}
ASSESSMENT_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assessment_id": {"type": "string"},
        "contract_id": {"type": "string"},
        "risk_level": {"type": "string"},
    },
    "required": ["assessment_id", "contract_id", "risk_level"],
}
SIGNATURE_PROPOSAL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "contract_id": CONTRACT_ID,
        "deal_value": {"type": "number", "minimum": 0},
        "signer_email": EMAIL,
    },
    "required": ["contract_id", "deal_value", "signer_email"],
    "additionalProperties": False,
}
SIGNATURE_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"envelope_id": {"type": "string"}, "contract_id": {"type": "string"}},
    "required": ["envelope_id", "contract_id"],
}

MANIFESTS: tuple[ToolManifest, ...] = (
    ToolManifest(
        name="clm.get_contract",
        writes=(),
        version="1.0.0",
        field_classes={"contract_id": "typed"},
        description="Look up a contract's record: title, counterparty, deal value and status.",
        input_schema={
            "type": "object",
            "properties": {"contract_id": CONTRACT_ID},
            "required": ["contract_id"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "contract_id": {"type": "string"},
                "title": {"type": "string"},
                "counterparty": {"type": "string"},
                "counterparty_contact": {"type": "string"},
                "deal_value": {"type": "number"},
                "status": {"type": "string"},
            },
            "required": ["contract_id", "title", "counterparty", "deal_value", "status"],
        },
        side_effect_class="read",
        risk_tier="low",
    ),
    ToolManifest(
        name="crm.write_assessment",
        writes=("crm.assessment",),
        version="1.0.0",
        description="Record the risk assessment for a contract in the CRM.",
        input_schema=ASSESSMENT_PROPOSAL,
        field_classes=ASSESSMENT_FIELDS,
        output_schema=ASSESSMENT_OUTPUT,
        side_effect_class="write",
        risk_tier="medium",
        postcondition="crm.assessment_matches",
        retired=True,
    ),
    ToolManifest(
        name="crm.write_assessment",
        writes=("crm.assessment",),
        version="2.0.0",
        description="Record the risk assessment for a contract in the CRM.",
        preparation_required=True,
        record_binding="clm.contract",
        proposal_schema=ASSESSMENT_PROPOSAL,
        input_schema=bound(ASSESSMENT_PROPOSAL, "contract_version"),
        field_classes={**ASSESSMENT_FIELDS, **BOUND_VERSION},
        output_schema=ASSESSMENT_OUTPUT,
        side_effect_class="write",
        risk_tier="medium",
        postcondition="crm.assessment_matches",
        retired=True,
    ),
    ToolManifest(
        name="crm.write_assessment",
        writes=("crm.assessment",),
        version="3.0.0",
        description="Record the risk assessment for a contract in the CRM.",
        preparation_required=True,
        record_binding="clm.contract",
        proposal_schema=ASSESSMENT_PROPOSAL,
        input_schema=with_prior(bound(ASSESSMENT_PROPOSAL, "contract_version")),
        field_classes={**ASSESSMENT_FIELDS, **BOUND_VERSION, **PRIOR_FIELDS},
        output_schema=ASSESSMENT_OUTPUT,
        side_effect_class="write",
        risk_tier="medium",
        postcondition="crm.assessment_matches",
    ),
    ToolManifest(
        name="email.send",
        writes=("email.sent",),
        version="2.0.0",
        field_classes={
            "to": "typed",
            "delivery.mailboxes": "typed",
            "delivery.directory_marker": "typed",
            "delivery.reapproval_required": "typed",
            "subject": "free_text",
            "body": "free_text",
        },
        description="Send email to a target expanded through the directory.",
        preparation_required=True,
        proposal_schema={
            "type": "object",
            "properties": {
                "to": EMAIL,
                "subject": {"type": "string", "maxLength": 200},
                "body": {"type": "string", "maxLength": 5000},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "to": EMAIL,
                "subject": {"type": "string", "maxLength": 200},
                "body": {"type": "string", "maxLength": 5000},
                "delivery": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "mailboxes": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 100,
                            "uniqueItems": True,
                            "items": EMAIL,
                        },
                        "directory_marker": {"type": "string", "minLength": 1},
                        "reapproval_required": {"type": "boolean"},
                    },
                    "required": ["mailboxes", "directory_marker", "reapproval_required"],
                },
            },
            "required": ["to", "subject", "body", "delivery"],
        },
        output_schema={
            "type": "object",
            "properties": {"message_id": {"type": "string"}, "to": {"type": "string"}},
            "required": ["message_id", "to"],
        },
        side_effect_class="external",
        risk_tier="high",
        required_roles=("case_manager", "legal_reviewer"),
        postcondition="email.sent_to_requested",
    ),
    ToolManifest(
        name="esign.send_for_signature",
        writes=("esign.envelope",),
        version="1.0.0",
        description="Send a contract to a signer for electronic signature.",
        input_schema=SIGNATURE_PROPOSAL,
        field_classes=SIGNATURE_FIELDS,
        output_schema=SIGNATURE_OUTPUT,
        side_effect_class="external",
        risk_tier="high",
        required_roles=("case_manager",),
        postcondition="esign.envelope_matches",
        retired=True,
    ),
    ToolManifest(
        name="esign.send_for_signature",
        writes=("esign.envelope",),
        version="2.0.0",
        description="Send a contract to a signer for electronic signature.",
        preparation_required=True,
        record_binding="clm.contract",
        proposal_schema=SIGNATURE_PROPOSAL,
        input_schema=bound(SIGNATURE_PROPOSAL, "contract_version"),
        field_classes={**SIGNATURE_FIELDS, **BOUND_VERSION},
        output_schema=SIGNATURE_OUTPUT,
        side_effect_class="external",
        risk_tier="high",
        required_roles=("case_manager",),
        postcondition="esign.envelope_matches",
    ),
)

RESTORE_PROPOSAL = {
    **ASSESSMENT_PROPOSAL,
    "properties": {
        **ASSESSMENT_PROPOSAL["properties"],
        "risk_level": {"enum": ["none", "low", "medium", "high"]},
        "undo_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    "required": [*ASSESSMENT_PROPOSAL["required"], "undo_id"],
}
MANIFESTS += (
    ToolManifest(
        name="crm.restore_assessment",
        version="1.0.0",
        trusted_only=True,
        description="Restore the rating captured before an earlier action.",
        writes=("crm.assessment.restore",),
        preparation_required=True,
        record_binding="clm.contract",
        proposal_schema=bound(RESTORE_PROPOSAL, "contract_version"),
        input_schema=bound(RESTORE_PROPOSAL, "contract_version"),
        field_classes={**ASSESSMENT_FIELDS, **BOUND_VERSION, "undo_id": "typed"},
        output_schema={
            "type": "object",
            "properties": {
                "restore_id": {"type": "string"},
                "contract_id": {"type": "string"},
                "restored_level": {"type": ["string", "null"]},
            },
            "required": ["restore_id", "contract_id", "restored_level"],
        },
        side_effect_class="write",
        risk_tier="medium",
        required_roles=("case_manager",),
    ),
)

POSTCONDITIONS: Mapping[str, PostCondition] = {
    "crm.assessment_matches": lambda args, out: (
        out.get("contract_id") == args.get("contract_id")
        and out.get("risk_level") == args.get("risk_level")
    ),
    "email.sent_to_requested": lambda args, out: out.get("to") == args.get("to"),
    "esign.envelope_matches": lambda args, out: out.get("contract_id") == args.get("contract_id"),
}


# What each tool's credential may do (M23): only what the tool itself does.
TOOL_SCOPES: dict[str, frozenset[str]] = {
    "clm.get_contract": frozenset({"contract:read"}),
    "crm.restore_assessment": frozenset({"assessment:restore"}),
    "crm.write_assessment": frozenset({"assessment:write"}),
    "email.send": frozenset({"mail:send"}),
    "esign.send_for_signature": frozenset({"envelope:send"}),
}
# The scope each declared write needs (startup checks the tools' scopes against them).
WRITE_SCOPES: dict[str, str] = {
    "crm.assessment": "assessment:write",
    "crm.assessment.restore": "assessment:restore",
    "email.sent": "mail:send",
    "esign.envelope": "envelope:send",
}


def tool_clients(clm: MockCLM, email: MockEmail, esign: MockESign) -> dict[str, ScopedClient]:
    """Each tool's client: its system, bound to the tool's own credential."""
    system: dict[str, ScopedSystem] = {
        "clm.get_contract": clm,
        "crm.write_assessment": clm,
        "crm.restore_assessment": clm,
        "email.send": email,
        "esign.send_for_signature": esign,
    }
    return {
        tool: ScopedClient(system[tool], Credential(f"gawe-tool:{tool}", scopes))
        for tool, scopes in TOOL_SCOPES.items()
    }


def implementations(
    clm: MockCLM, email: MockEmail, esign: MockESign
) -> dict[str, ToolImplementation]:
    clients = tool_clients(clm, email, esign)

    def get_contract(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        return clients["clm.get_contract"].get_contract(args["contract_id"])  # type: ignore[no-any-return]

    def write_assessment(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        # Only the current version (3.0.0) is ever sent; retired versions only verify.
        return clients["crm.write_assessment"].set_assessment(  # type: ignore[no-any-return]
            call.idempotency_key,
            args["contract_id"],
            args["risk_level"],
            args["summary"],
            contract_version=args["record"]["contract_version"],
        )

    def restore_assessment(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        prior = (
            None
            if args["risk_level"] == NO_RATING
            else {"risk_level": args["risk_level"], "summary": args["summary"]}
        )
        return clients["crm.restore_assessment"].restore_assessment(  # type: ignore[no-any-return]
            call.idempotency_key,
            args["contract_id"],
            prior,
            contract_version=args["record"]["contract_version"],
        )

    def send_email(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        return clients["email.send"].send_prepared(call.idempotency_key, args)  # type: ignore[no-any-return]

    def send_for_signature(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        # No separate read first: revision and deal value are checked inside the write.
        return clients["esign.send_for_signature"].send_for_signature(  # type: ignore[no-any-return]
            call.idempotency_key,
            args["contract_id"],
            args["signer_email"],
            args["deal_value"],
            contract_version=args["record"]["contract_version"],
        )

    return {
        "clm.get_contract": get_contract,
        "crm.write_assessment": write_assessment,
        "crm.restore_assessment": restore_assessment,
        "email.send": send_email,
        "esign.send_for_signature": send_for_signature,
    }


# M27: versioned snapshots; preceding versions still serve persisted plans/approvals.

MANIFESTS += (
    with_snapshots(
        next(m for m in MANIFESTS if m.name == "crm.write_assessment" and m.version == "3.0.0"),
        "4.0.0",
        maximum=1,
    ),
)
MANIFESTS += (
    with_snapshots(
        next(m for m in MANIFESTS if m.name == "esign.send_for_signature" and m.version == "2.0.0"),
        "3.0.0",
        maximum=1,
    ),
)

# S4: only the trusted undo service supplies these arguments, including original evidence.
RESTORE_2 = with_snapshots(
    next(m for m in MANIFESTS if m.name == "crm.restore_assessment" and m.version == "1.0.0"),
    "2.0.0",
    minimum=0,
)
MANIFESTS += (RESTORE_2.model_copy(update={"proposal_schema": RESTORE_2.input_schema}),)
