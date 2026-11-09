import pytest
from pydantic import ValidationError
from tests.conftest import make_manifest

from gawe.core.registry import (
    DuplicateToolError,
    InvalidManifestError,
    ToolNotFoundError,
    ToolRegistry,
)


def test_register_and_get() -> None:
    reg = ToolRegistry()
    reg.register(make_manifest())
    assert reg.get("contracts.send_email").version == "1.0.0"
    assert "contracts.send_email" in reg
    assert "nope" not in reg


def test_latest_version_uses_numeric_ordering() -> None:
    reg = ToolRegistry()
    for v in ("1.9.0", "1.10.0", "1.2.0"):
        reg.register(make_manifest(version=v))
    assert reg.get("contracts.send_email").version == "1.10.0"
    assert reg.get("contracts.send_email", "1.2.0").version == "1.2.0"


def test_duplicate_version_rejected() -> None:
    reg = ToolRegistry()
    reg.register(make_manifest())
    with pytest.raises(DuplicateToolError):
        reg.register(make_manifest())


def test_unknown_tool_and_version() -> None:
    reg = ToolRegistry()
    reg.register(make_manifest())
    with pytest.raises(ToolNotFoundError):
        reg.get("missing")
    with pytest.raises(ToolNotFoundError):
        reg.get("contracts.send_email", "9.9.9")


def test_invalid_json_schema_rejected() -> None:
    reg = ToolRegistry()
    with pytest.raises(InvalidManifestError):
        reg.register(
            make_manifest(
                input_schema={"type": "object", "properties": {"a": {"type": "nonsense"}}}
            )
        )


def test_non_object_schema_rejected() -> None:
    reg = ToolRegistry()
    with pytest.raises(InvalidManifestError):
        reg.register(make_manifest(input_schema={"type": "string"}))


def test_list_returns_latest_of_each_sorted() -> None:
    reg = ToolRegistry()
    reg.register(make_manifest("b.tool", "1.0.0"))
    reg.register(make_manifest("a.tool", "1.0.0"))
    reg.register(make_manifest("a.tool", "2.0.0"))
    assert [(m.name, m.version) for m in reg.list()] == [("a.tool", "2.0.0"), ("b.tool", "1.0.0")]


def test_empty_registry_lists_nothing() -> None:
    assert ToolRegistry().list() == []


@pytest.mark.parametrize("bad_version", ["1.0", "v1.0.0", "1.0.0-beta", ""])
def test_bad_version_rejected_by_model(bad_version: str) -> None:
    with pytest.raises(ValidationError):
        make_manifest(version=bad_version)


def test_manifest_is_frozen_and_forbids_extras() -> None:
    manifest = make_manifest()
    with pytest.raises(ValidationError):
        manifest.name = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        make_manifest(surprise="field")
