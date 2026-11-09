import pytest

from gawe.core.models import ToolManifest
from gawe.core.registry import ToolRegistry


def make_manifest(name: str = "contracts.send_email", version: str = "1.0.0", **overrides):  # type: ignore[no-untyped-def]
    base = {
        "name": name,
        "version": version,
        "input_schema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
            "required": ["to", "body"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        "side_effect_class": "external",
        "risk_tier": "high",
        "required_roles": ("legal_reviewer",),
    }
    base.update(overrides)
    return ToolManifest(**base)


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(make_manifest())
    reg.register(
        make_manifest(
            "contracts.extract_risks",
            input_schema={
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            side_effect_class="read",
            risk_tier="low",
            required_roles=(),
        )
    )
    return reg
