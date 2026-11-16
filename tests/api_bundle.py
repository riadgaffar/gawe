"""An AppBundle built from the shared test tools, for wiring and HTTP tests."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from gawe.api.wiring import AppBundle
from gawe.core.policy.provenance import PolicySource, PolicyVariable
from gawe.core.readback import ReadbackResult
from tests.engine_flow import TENANT_VARS
from tests.flow import FIXTURES, FakeConnector, manifests


def bundle(connector: FakeConnector | None = None) -> tuple[AppBundle, FakeConnector]:
    connector = connector or FakeConnector()
    catalog = manifests()

    def read(key: str, args: Mapping[str, Any], now: datetime) -> ReadbackResult:
        output = connector.effects.get(key)
        return ReadbackResult("agreement" if output else "unavailable", output=output)

    return (
        AppBundle(
            manifests=list(catalog.values()),
            tools={name: connector for name in catalog},
            readbacks={
                (m.name, m.version): read for m in catalog.values() if m.side_effect_class != "read"
            },
            policy_rules=FIXTURES / "example_policies.yaml",
            profiles=frozenset({"contract_review"}),
            variables=lambda run: _vars(),
            policy_sources={
                "tenant_config": PolicySource(kind="configuration", resource="config.tenant"),
            },
            policy_variables={
                "tenant.internal_domains": PolicyVariable(dependencies=("tenant_config",)),
            },
        ),
        connector,
    )


def _vars() -> dict[str, Any]:
    return TENANT_VARS
