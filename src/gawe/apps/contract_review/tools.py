"""The four Contract Review tools: their manifests, implementations and post-conditions.

Policy decides what may run; these only describe and perform the work. Two of them defend
themselves as well:

* `esign.send_for_signature` takes `deal_value` as an argument because the approval rules need to
  see it, but the model supplies arguments, so the tool checks the value against the contract
  system and refuses on a mismatch. A planner that understates the value to dodge the finance
  approval gets a refused call, not a signature request.
* Every tool echoes what it acted on, and a post-condition compares that with what was asked, so a
  call that "succeeds" against the wrong thing fails verification.
"""

from collections.abc import Mapping
from typing import Any

from gawe.connectors.base import ConnectorError
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_email import MockEmail
from gawe.connectors.mock_esign import MockESign
from gawe.core.executor import ToolCall, ToolImplementation
from gawe.core.models import ToolManifest
from gawe.core.verifier import PostCondition

CONTRACT_ID = {"type": "string", "pattern": r"^C-\d{4}$"}
EMAIL = {"type": "string", "maxLength": 254}

MANIFESTS = (
    ToolManifest(
        name="clm.get_contract",
        writes=(),
        version="1.0.0",
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
        input_schema={
            "type": "object",
            "properties": {
                "contract_id": CONTRACT_ID,
                "risk_level": {"enum": ["low", "medium", "high"]},
                "summary": {"type": "string", "maxLength": 500},
            },
            "required": ["contract_id", "risk_level", "summary"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "assessment_id": {"type": "string"},
                "contract_id": {"type": "string"},
                "risk_level": {"type": "string"},
            },
            "required": ["assessment_id", "contract_id", "risk_level"],
        },
        side_effect_class="write",
        risk_tier="medium",
        postcondition="crm.assessment_matches",
    ),
    ToolManifest(
        name="email.send",
        writes=("email.sent",),
        version="1.0.0",
        description="Send an email to one recipient.",
        input_schema={
            "type": "object",
            "properties": {
                "to": EMAIL,
                "subject": {"type": "string", "maxLength": 200},
                "body": {"type": "string", "maxLength": 5000},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
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
        input_schema={
            "type": "object",
            "properties": {
                "contract_id": CONTRACT_ID,
                "deal_value": {"type": "number", "minimum": 0},
                "signer_email": EMAIL,
            },
            "required": ["contract_id", "deal_value", "signer_email"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"envelope_id": {"type": "string"}, "contract_id": {"type": "string"}},
            "required": ["envelope_id", "contract_id"],
        },
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
            call.idempotency_key, args["contract_id"], args["risk_level"], args["summary"]
        )

    def send_email(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        return email.send(call.idempotency_key, args["to"], args["subject"], args["body"])

    def send_for_signature(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        record = clm.get_contract(args["contract_id"])
        if float(record["deal_value"]) != float(args["deal_value"]):
            raise ConnectorError("deal_value_mismatch")
        return esign.send_for_signature(
            call.idempotency_key, args["contract_id"], args["signer_email"], args["deal_value"]
        )

    return {
        "clm.get_contract": get_contract,
        "crm.write_assessment": write_assessment,
        "email.send": send_email,
        "esign.send_for_signature": send_for_signature,
    }
