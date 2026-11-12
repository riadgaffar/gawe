"""Startup rejects writable policy inputs; real mocks preserve declared safe inputs."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.helpers import signer, tool

from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, load_app_rules, memory_stores
from gawe.apps import contract_review, it_access
from gawe.core.executor import ToolCall
from gawe.core.policy.provenance import PolicySource, PolicyVariable, validate_provenance
from gawe.core.policy.rules import Rule, RuleConfigError
from gawe.core.runs import RunRecord
from gawe.llm.fake import FakeProvider
from gawe.llm.planner import _entry

SOURCE = PolicySource(kind="resource", resource="clm.contract.deal_value")
VARIABLE = PolicyVariable(dependencies=("deal_values",))
WRITE = tool("crm.write", "write", "medium", {"value": {"type": "string"}})


def test_absent_write_metadata_is_not_an_explicit_empty_set() -> None:
    with pytest.raises(RuleConfigError, match="missing write-resource"):
        validate_provenance([], [WRITE.model_copy(update={"writes": None})], {}, {})
    validate_provenance([], [WRITE.model_copy(update={"writes": ()})], {}, {})
    validate_provenance([], [], {}, {})


@pytest.mark.parametrize(
    "bad", ["", "crm.*", "crm..value", "crm.", ".crm", "CRM", "crm/value", "crm\n"]
)
@pytest.mark.parametrize("location", ["write", "source", "variable", "dependency", "resource"])
def test_noncanonical_names_are_rejected(bad: str, location: str) -> None:
    manifest = WRITE
    sources = {"deal_values": SOURCE}
    variables = {"review.large_deals": VARIABLE}
    if location == "write":
        manifest = WRITE.model_copy(update={"writes": (bad,)})
    elif location == "source":
        sources[bad] = SOURCE
    elif location == "variable":
        variables[bad] = VARIABLE
    elif location == "dependency":
        variables["review.large_deals"] = PolicyVariable(dependencies=(bad,))
    else:
        sources["deal_values"] = PolicySource(kind="resource", resource=bad)
    with pytest.raises(RuleConfigError, match="invalid provenance name"):
        validate_provenance([], [manifest], sources, variables)


def test_read_tools_cannot_declare_writes() -> None:
    with pytest.raises(RuleConfigError, match="read tool declares writes"):
        validate_provenance([], [WRITE.model_copy(update={"side_effect_class": "read"})], {}, {})


@pytest.mark.parametrize(
    "variables, message",
    [
        ({"value": PolicyVariable(dependencies=())}, "empty provenance"),
        ({"value": PolicyVariable(dependencies=("missing",))}, "unknown provenance"),
        ({"value": PolicyVariable(dependencies=("value",))}, "cyclic"),
        (
            {"a": PolicyVariable(dependencies=("b",)), "b": PolicyVariable(dependencies=("a",))},
            "cyclic",
        ),
        ({"deal_values": VARIABLE}, "must be disjoint"),
    ],
)
def test_invalid_graphs_fail_even_without_policy_references(variables, message) -> None:
    with pytest.raises(RuleConfigError, match=message):
        validate_provenance([], [WRITE], {"deal_values": SOURCE}, variables)


@pytest.mark.parametrize(
    "write", ["clm", "clm.contract", "clm.contract.deal_value", "clm.contract.deal_value.amount"]
)
@pytest.mark.parametrize("kind", ["configuration", "findings", "resource"])
def test_transitive_overlap_rejects_all_leaf_kinds_and_namespace_directions(write, kind) -> None:
    sources = {"deal_values": PolicySource(kind=kind, resource=SOURCE.resource)}
    variables = {
        "review.large_deals": PolicyVariable(dependencies=("intermediate",)),
        "intermediate": VARIABLE,
    }
    with pytest.raises(RuleConfigError, match="review.large_deals.*overlaps.*crm.write@1.0.0"):
        validate_provenance([], [WRITE.model_copy(update={"writes": (write,)})], sources, variables)


@pytest.mark.parametrize(
    "write", ["crm.assessment", "clm.contract.status", "clm.contract.deal_values", "clm.contracts"]
)
def test_safe_siblings_and_shared_dependency_dag_are_valid(write: str) -> None:
    variables = {
        "left": VARIABLE,
        "right": VARIABLE,
        "review.large_deals": PolicyVariable(dependencies=("left", "right")),
    }
    validate_provenance(
        [], [WRITE.model_copy(update={"writes": (write,)})], {"deal_values": SOURCE}, variables
    )


@pytest.mark.parametrize("operand", ["$missing", "$deal_values", "$", "$review.large_deals"])
def test_every_variable_operand_requires_a_variable_declaration(operand: str) -> None:
    rule = Rule.model_validate(
        {
            "id": "inactive",
            "effect": "deny",
            "when": {"tool": "crm.write", "args": {"value": {"eq": operand}}},
        }
    )
    with pytest.raises(RuleConfigError, match="inactive.*undeclared policy variable"):
        validate_provenance([rule], [WRITE], {"deal_values": SOURCE}, {})
    if operand == "$review.large_deals":
        validate_provenance(
            [rule], [WRITE], {"deal_values": SOURCE}, {"review.large_deals": VARIABLE}
        )


@pytest.mark.parametrize("unsafe_version", ["1.0.0", "2.0.0"])
def test_assembly_rejects_deal_value_writes_in_every_registered_version(
    tmp_path, unsafe_version
) -> None:
    bundle = contract_review.bundle(tmp_path)
    original = next(m for m in bundle.manifests if m.name == "crm.write_assessment")
    versions = [
        original.model_copy(
            update={
                "version": version,
                "writes": ("clm.contract.deal_value",)
                if version == unsafe_version
                else original.writes,
            }
        )
        for version in ("2.0.0", "1.0.0")
    ]
    bundle = replace(
        bundle, manifests=[*(m for m in bundle.manifests if m.name != original.name), *versions]
    )
    provider = FakeProvider([])
    with pytest.raises(
        RuleConfigError, match=f"review.large_deals.*crm.write_assessment@{unsafe_version}"
    ):
        assemble(
            bundle,
            stores=memory_stores(),
            signer=signer(),
            provider=provider,
            background=InlineBackground(),
        )
    assert not (tmp_path / "ledger.jsonl").exists()


@pytest.mark.parametrize("app", [contract_review, it_access])
def test_apps_require_metadata_and_declarations_at_entry_point(tmp_path, app) -> None:
    bundle = app.bundle(tmp_path)
    assert load_app_rules(bundle)
    with pytest.raises(RuleConfigError, match="undeclared policy variable"):
        load_app_rules(replace(bundle, policy_variables={}))
    with pytest.raises(RuleConfigError, match="missing write-resource"):
        load_app_rules(
            replace(
                bundle,
                manifests=[
                    bundle.manifests[0].model_copy(update={"writes": None}),
                    *bundle.manifests[1:],
                ],
            )
        )
    assert all("writes" not in _entry(m) for m in bundle.manifests)


@pytest.mark.parametrize("app", [contract_review, it_access])
def test_safe_callbacks_remain_unchanged_after_real_mock_writes(tmp_path: Path, app) -> None:
    bundle = app.bundle(tmp_path)
    run = RunRecord(
        run_id="fixed",
        tenant_id="t",
        acting_user_id="u",
        objective="Check sources",
        profile=app.PROFILE_NAME,
        created_at=datetime.now(UTC),
        findings={
            "requester_email": "dana.ortiz@acme.example",
            "flagged_clauses": [{"severity": "high"}],
        },
    )
    before = deepcopy(bundle.variables(run))
    actions = {
        "crm.write_assessment": {
            "contract_id": "C-1002",
            "risk_level": "low",
            "summary": "Changed assessment",
        },
        "email.send": {"to": "legal@acme.example", "subject": "Review", "body": "Review"},
        "esign.send_for_signature": {
            "contract_id": "C-1001",
            "deal_value": 45000,
            "signer_email": "legal@acme.example",
        },
        "iam.grant_access": {"email": "dana.ortiz@acme.example", "system": "wiki", "level": "read"},
    }
    count = 0
    for manifest in bundle.manifests:
        if manifest.writes:
            count += 1
            call = ToolCall(
                run_id="fixed", step_id=f"s{count}", attempt=1, idempotency_key=f"key{count}"
            )
            assert bundle.tools[manifest.name](actions[manifest.name], call)
            assert bundle.variables(run) == before
    assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == count
