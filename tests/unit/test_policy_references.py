"""Application policy loading: schema drift must fail before any run starts."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.helpers import TOOLS, signer, tool

from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, memory_stores
from gawe.apps import contract_review, it_access
from gawe.core.models import ToolManifest
from gawe.core.policy.rules import Rule, RuleConfigError, load_rules, validate_rule_references
from gawe.llm.fake import FakeProvider

TOOLS_WITH_VALUE = (
    tool("contracts.read", "read", "low", {"value": {"type": "string"}}),
    tool("contracts.write", "write", "high", {"value": {"enum": ["low", "high"]}}),
)


def rule(**when: Any) -> Rule:
    return Rule.model_validate({"id": "schema-check", "effect": "deny", "when": when})


@pytest.mark.parametrize(
    "selectors",
    [
        {"tool": "contracts.read"},
        {"tool": "contracts.*"},
        {"tool": "contracts.[rw]*"},
        {"tool": "*", "side_effect_class": "read", "risk_tier": "low"},
        {"side_effect_class": "read"},
        {"risk_tier": "high"},
        {},
    ],
)
def test_paths_are_checked_for_all_selector_forms(selectors: dict[str, Any]) -> None:
    validate_rule_references(
        [rule(**selectors, args={"value": {"eq": "$runtime.value"}})], TOOLS_WITH_VALUE
    )


@pytest.mark.parametrize(
    "selectors, message",
    [
        ({"tool": "missing.tool"}, "matches no registered tools"),
        ({"tool": "missing.*"}, "matches no registered tools"),
        ({"tool": "CONTRACTS.*"}, "matches no registered tools"),
        ({"tool": "missing.*", "risk_tier": "medium"}, "matches no registered tools"),
        ({"side_effect_class": "external"}, "no tools match selectors"),
        ({"risk_tier": "medium"}, "no tools match selectors"),
        ({"tool": "contracts.read", "risk_tier": "high"}, "no tools match selectors"),
        ({"side_effect_class": "read", "risk_tier": "high"}, "no tools match selectors"),
    ],
)
def test_dead_selectors_are_rejected(selectors: dict[str, Any], message: str) -> None:
    with pytest.raises(RuleConfigError, match=message) as error:
        validate_rule_references([rule(**selectors)], TOOLS_WITH_VALUE)
    assert "schema-check" in str(error.value)


@pytest.mark.parametrize("selectors", [{"tool": "contracts.*"}, {"risk_tier": "low"}, {}])
def test_every_matching_tool_must_declare_the_path(selectors: dict[str, Any]) -> None:
    missing = tool("contracts.other", "read", "low", {})
    with pytest.raises(RuleConfigError, match="property 'value' is not declared") as error:
        validate_rule_references(
            [rule(**selectors, args={"value": {"eq": "x"}})], [*TOOLS_WITH_VALUE, missing]
        )
    assert "schema-check" in str(error.value)
    assert "contracts.other@1.0.0" in str(error.value)
    assert "arg 'value'" in str(error.value)


def test_metadata_filters_exclude_tools_without_the_path() -> None:
    validate_rule_references(
        [rule(tool="contracts.*", side_effect_class="read", args={"value": {"eq": "x"}})],
        [TOOLS_WITH_VALUE[0], tool("contracts.other", "write", "high", {})],
    )


@pytest.mark.parametrize("path", ["", ".value", "value.", "value..nested"])
def test_empty_path_components_are_rejected(path: str) -> None:
    with pytest.raises(RuleConfigError, match="empty path component"):
        validate_rule_references([rule(args={path: {"exists": True}})], TOOLS_WITH_VALUE)


def check_path(schema: dict[str, Any], path: str) -> None:
    manifest = TOOLS_WITH_VALUE[0].model_copy(update={"input_schema": schema})
    validate_rule_references([rule(args={path: {"exists": True}})], [manifest])


def test_nested_optional_properties_and_enum_leaves_are_declared() -> None:
    check_path(
        {
            "type": "object",
            "properties": {
                "nested": {"type": "object", "properties": {"0": {"enum": ["low", "high"]}}}
            },
        },
        "nested.0",
    )


@pytest.mark.parametrize(
    "schema, path, message",
    [
        ({"type": "object"}, "missing", "not declared"),
        ({"type": "object", "additionalProperties": True}, "missing", "not declared"),
        ({"type": "object", "patternProperties": {".*": {}}}, "missing", "not declared"),
        ({"type": "object", "properties": {"x": {"type": "string"}}}, "x.y", "type 'object'"),
        ({"type": "object", "properties": {"x": True}}, "x.y", "non-object"),
        (
            {"type": "object", "properties": {"x": {"properties": {"y": {}}}}},
            "x.y",
            "type 'object'",
        ),
        (
            {"type": "object", "properties": {"x": {"type": "array", "items": {"type": "string"}}}},
            "x.0",
            "type 'object'",
        ),
    ],
)
def test_unprovable_paths_are_rejected(schema: dict[str, Any], path: str, message: str) -> None:
    with pytest.raises(RuleConfigError, match=message):
        check_path(schema, path)


@pytest.mark.parametrize(
    "structure",
    [
        {"$ref": "#/$defs/object"},
        {"$dynamicRef": "#object"},
        {"allOf": [{"properties": {"value": {}}}]},
        {"anyOf": [{"properties": {"value": {}}}, {}]},
        {"oneOf": [{"properties": {"value": {}}}, {}]},
        {"not": {"required": ["value"]}},
        {"if": {"required": ["flag"]}, "then": {"properties": {"value": {}}}},
        {"dependentSchemas": {"flag": {"properties": {"value": {}}}}},
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_indirect_structure_on_path_is_rejected(structure: dict[str, Any], nested: bool) -> None:
    schema = {"type": "object", **structure}
    if nested:
        schema = {"type": "object", "properties": {"nested": schema}}
    with pytest.raises(RuleConfigError, match="unsupported path schema structure"):
        check_path(schema, "nested.value" if nested else "value")


def test_unrelated_structure_and_leaf_constraints_do_not_require_resolution() -> None:
    check_path(
        {
            "type": "object",
            "properties": {
                "value": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "other": {"$ref": "#/$defs/other"},
            },
        },
        "value",
    )


def test_empty_policy_is_valid_even_without_tools() -> None:
    validate_rule_references([], [])
    with pytest.raises(RuleConfigError, match="no tools match selectors"):
        validate_rule_references([rule(args={"value": {"exists": True}})], [])


@pytest.mark.parametrize("app", ["contract_review", "it_access", "example"])
def test_every_shipped_policy_loads_with_its_manifests(app: str) -> None:
    if app == "example":
        path = Path(__file__).parents[1] / "fixtures" / "example_policies.yaml"
        manifests = tuple(TOOLS.values())
    else:
        module = contract_review if app == "contract_review" else it_access
        path = module.HERE / "policies.yaml"
        manifests = tuple(module.MANIFESTS)
    assert load_rules(path, manifests=manifests)


def test_file_loading_cannot_skip_reference_validation(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("rules: [{id: stale, effect: deny, when: {tool: stale.tool}}]")
    with pytest.raises(RuleConfigError, match="stale.tool"):
        load_rules(path, manifests=TOOLS_WITH_VALUE)


def renamed_assessment(manifest: ToolManifest) -> ToolManifest:
    schema = deepcopy(manifest.input_schema)
    schema["properties"]["rating"] = schema["properties"].pop("risk_level")
    schema["required"] = ["rating" if key == "risk_level" else key for key in schema["required"]]
    return manifest.model_copy(update={"input_schema": schema})


@pytest.mark.parametrize("renamed_version", [None, "1.0.0", "2.0.0"])
def test_assembly_checks_every_executable_tool_version(
    tmp_path: Path, renamed_version: str | None
) -> None:
    bundle = contract_review.bundle(tmp_path)
    original = next(m for m in bundle.manifests if m.name == "crm.write_assessment")
    versions = [
        (renamed_assessment(original) if version == renamed_version else original).model_copy(
            update={"version": version}
        )
        for version in ("2.0.0", "1.0.0")
    ]
    bundle = replace(
        bundle, manifests=[*(m for m in bundle.manifests if m.name != original.name), *versions]
    )
    kwargs = dict(
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider([]),
        background=InlineBackground(),
    )
    if renamed_version is not None:
        with pytest.raises(RuleConfigError) as error:
            assemble(bundle, **kwargs)
        assert "low-risk-contradicted-needs-legal" in str(error.value)
        assert f"crm.write_assessment@{renamed_version}" in str(error.value)
        assert "risk_level" in str(error.value)
    else:
        assert assemble(bundle, **kwargs)
    assert not (tmp_path / "ledger.jsonl").exists()
