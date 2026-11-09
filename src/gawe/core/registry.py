"""Tool registry: discovery and manifest integrity."""

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from gawe.core.models import ToolManifest


class RegistryError(Exception):
    """Base class for registry failures."""


class DuplicateToolError(RegistryError):
    pass


class InvalidManifestError(RegistryError):
    pass


class ToolNotFoundError(RegistryError):
    pass


def _version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return (major, minor, patch)


def _check_schema(label: str, schema: dict[str, object]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidManifestError(f"{label} is not a valid JSON Schema: {exc.message}") from exc
    if schema.get("type") != "object":
        raise InvalidManifestError(f"{label} must declare type 'object'")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, dict[str, ToolManifest]] = {}

    def register(self, manifest: ToolManifest) -> None:
        _check_schema(f"{manifest.name} input_schema", manifest.input_schema)
        _check_schema(f"{manifest.name} output_schema", manifest.output_schema)
        versions = self._tools.setdefault(manifest.name, {})
        if manifest.version in versions:
            raise DuplicateToolError(f"{manifest.name}@{manifest.version} already registered")
        versions[manifest.version] = manifest

    def get(self, name: str, version: str | None = None) -> ToolManifest:
        versions = self._tools.get(name)
        if not versions:
            raise ToolNotFoundError(f"unknown tool: {name}")
        if version is None:
            return versions[max(versions, key=_version_key)]
        try:
            return versions[version]
        except KeyError:
            raise ToolNotFoundError(f"unknown version: {name}@{version}") from None

    def list(self) -> list[ToolManifest]:
        """Latest version of every tool, sorted by name."""
        return [self.get(name) for name in sorted(self._tools)]

    def __contains__(self, name: object) -> bool:
        return name in self._tools
