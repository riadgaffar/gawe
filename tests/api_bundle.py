"""An AppBundle built from the shared test tools, for wiring and HTTP tests."""

from typing import Any

from gawe.api.wiring import AppBundle
from tests.engine_flow import TENANT_VARS
from tests.flow import FIXTURES, FakeConnector, manifests


def bundle(connector: FakeConnector | None = None) -> tuple[AppBundle, FakeConnector]:
    connector = connector or FakeConnector()
    catalog = manifests()
    return (
        AppBundle(
            manifests=list(catalog.values()),
            tools={name: connector for name in catalog},
            policy_rules=FIXTURES / "example_policies.yaml",
            profiles=frozenset({"contract_review"}),
            variables=lambda run: _vars(),
        ),
        connector,
    )


def _vars() -> dict[str, Any]:
    return TENANT_VARS
