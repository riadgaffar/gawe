"""Settings per tenant, from a file read once at startup (M29).

An app declares a JSON Schema for one tenant's settings and the defaults every tenant gets. A
deployment may give some tenants different values in a file named by `GAWE_TENANT_SETTINGS`:

    {"tenants": {"<tenant id>": {"<setting>": <value>, ...}, ...}}

Each tenant's values are merged over the defaults and the result is checked against the schema,
so an unknown setting, a wrong type or a value the schema forbids stops startup. The settings are
then frozen: no route, tool or maintenance command can change them, and a change means editing
the file and restarting. A tenant the file doesn't name gets the defaults.
"""

import copy
import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

SETTINGS_ENV = "GAWE_TENANT_SETTINGS"


class TenantSettingsError(ValueError):
    """The settings file or a tenant's settings are not valid. The message names what."""


def _frozen(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _frozen(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(v) for v in value)
    return value


def _plain(value: Any) -> Any:
    """A mutable, JSON-shaped copy: what policy and app code read."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value


class TenantSettings:
    """One app's settings: its defaults and any per-tenant values, checked and frozen."""

    def __init__(
        self,
        schema: Mapping[str, Any],
        defaults: Mapping[str, Any],
        tenants: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise TenantSettingsError(
                "a settings schema must be an object that allows no extra keys"
            )
        self._validator = Draft202012Validator(copy.deepcopy(dict(schema)))
        self._check("the defaults", defaults)
        self._defaults = _frozen(defaults)
        merged: dict[str, Any] = {}
        for tenant_id, values in (tenants or {}).items():
            if not isinstance(tenant_id, str) or not tenant_id:
                raise TenantSettingsError("a tenant id must be a non-empty string")
            if not isinstance(values, Mapping):
                raise TenantSettingsError(f"tenant {tenant_id}: settings must be an object")
            combined = {**_plain(self._defaults), **copy.deepcopy(dict(values))}
            self._check(f"tenant {tenant_id}", combined)
            merged[tenant_id] = _frozen(combined)
        self._tenants: Mapping[str, Any] = MappingProxyType(merged)

    def _check(self, what: str, values: Mapping[str, Any]) -> None:
        errors = sorted(self._validator.iter_errors(dict(values)), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            where = ".".join(str(p) for p in first.path) or "(top level)"
            raise TenantSettingsError(f"{what}: {where}: {first.message}")

    @property
    def tenants(self) -> frozenset[str]:
        """The tenants the file names; every other tenant gets the defaults."""
        return frozenset(self._tenants)

    def defaults(self) -> dict[str, Any]:
        return _plain(self._defaults)  # type: ignore[no-any-return]

    def for_tenant(self, tenant_id: str | None) -> dict[str, Any]:
        """A fresh copy of the tenant's settings, so no caller can change what others read."""
        found = self._tenants.get(tenant_id) if tenant_id is not None else None
        return _plain(found if found is not None else self._defaults)  # type: ignore[no-any-return]


def read_settings_file(path: Path) -> dict[str, dict[str, Any]]:
    """The per-tenant values in a settings file. Its shape is checked here; the values are
    checked by each app's schema."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TenantSettingsError(f"{path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"tenants"}
        or not isinstance(data["tenants"], dict)
    ):
        raise TenantSettingsError(f'{path}: expected {{"tenants": {{"<tenant id>": {{...}}}}}}')
    return data["tenants"]


def from_env(schema: Mapping[str, Any], defaults: Mapping[str, Any]) -> TenantSettings:
    """The app's settings, with per-tenant values from `GAWE_TENANT_SETTINGS` when it is set."""
    raw = os.environ.get(SETTINGS_ENV, "").strip()
    tenants = read_settings_file(Path(raw)) if raw else None
    return TenantSettings(schema, defaults, tenants)
