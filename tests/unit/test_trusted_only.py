"""Trusted tools remain usable by the engine but unreachable by model plans."""

import json

from gawe.core.models import ToolManifest
from gawe.core.registry import ToolRegistry
from gawe.core.validator import validate_raw_plan
from gawe.llm.planner import _catalog


def test_trusted_tool_is_hidden_and_rejected() -> None:
    registry = ToolRegistry()
    for name, trusted in (("crm.restore_assessment", True), ("crm.read", False)):
        registry.register(
            ToolManifest(
                name=name,
                version="1.0.0",
                trusted_only=trusted,
                field_classes={},
                input_schema={"type": "object", "additionalProperties": False},
                output_schema={"type": "object"},
                side_effect_class="read",
                risk_tier="low",
            )
        )
    assert [m["name"] for m in json.loads(_catalog(registry))] == ["crm.read"]
    assert registry.get("crm.restore_assessment").trusted_only
    result = validate_raw_plan(
        {"steps": [{"id": "restore", "tool": "crm.restore_assessment"}]}, registry
    )
    assert not result.ok
    assert [i.code for i in result.issues] == ["UNKNOWN_TOOL"]
