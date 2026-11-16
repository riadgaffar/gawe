"""The four Contract Review tools: their manifests, implementations and post-conditions.

Policy decides what may run; these only describe and perform the work. Two of them defend
themselves as well:

* `esign.send_for_signature` takes `deal_value` as an argument because the approval rules need to
  see it, but the model supplies arguments, so the write checks the value against the contract
  system and refuses on a mismatch. A planner that understates the value to dodge the finance
  approval gets a refused call, not a signature request.
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

from gawe.apps.record_binding import bound
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

MANIFESTS = (
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

POSTCONDITIONS: Mapping[str, PostCondition] = {
    "crm.assessment_matches": lambda args, out: (
        out.get("contract_id") == args.get("contract_id")
        and out.get("risk_level") == args.get("risk_level")
    ),
    "email.sent_to_requested": lambda args, out: out.get("to") == args.get("to"),
    "esign.envelope_matches": lambda args, out: out.get("contract_id") == args.get("contract_id"),
}


def implementations(
    clm: MockCLM, email: MockEmail, esign: MockESign
) -> dict[str, ToolImplementation]:
    def get_contract(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        return clm.get_contract(args["contract_id"])

    def write_assessment(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        return clm.write_assessment(
            call.idempotency_key,
            args["contract_id"],
            args["risk_level"],
            args["summary"],
            contract_version=args["record"]["contract_version"],
        )

    def send_email(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        return email.send_prepared(call.idempotency_key, args)

    def send_for_signature(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        # No separate read first: revision and deal value are checked inside the write.
        return esign.send_for_signature(
            call.idempotency_key,
            args["contract_id"],
            args["signer_email"],
            args["deal_value"],
            contract_version=args["record"]["contract_version"],
        )

    return {
        "clm.get_contract": get_contract,
        "crm.write_assessment": write_assessment,
        "email.send": send_email,
        "esign.send_for_signature": send_for_signature,
    }
