"""M23: each tool's code holds a credential scoped to what the tool does; the system refuses the
rest. A read credential can't write: tried here with every tool's credential against every
operation outside it."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gawe.api.wiring import load_app_rules
from gawe.apps import contract_review, event_replay, it_access
from gawe.apps.contract_review import tools as cr_tools
from gawe.apps.event_replay import tools as er_tools
from gawe.apps.it_access import tools as it_tools
from gawe.connectors.base import Ledger
from gawe.connectors.credentials import CredentialScopeError, ScopedClient, is_read_scope
from gawe.connectors.mock_billing import MockBilling
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_directory import MockDirectory
from gawe.connectors.mock_email import MockEmail
from gawe.connectors.mock_esign import MockESign
from gawe.connectors.mock_iam import MockIAM


class Systems:
    """The mock systems of both apps over one ledger, and every tool's client."""

    def __init__(self, path: Path) -> None:
        self.ledger = Ledger(path / "ledger.jsonl")
        self.clm = MockCLM.from_seed(contract_review.HERE / "samples/clm_seed.json", self.ledger)
        self.esign = MockESign(self.ledger, self.clm)
        self.mail = MockEmail(self.ledger)
        self.mail.configure(
            MockDirectory.from_seed(
                path, Path(contract_review.__file__).parents[2] / "connectors/seeds/directory.json"
            )
        )
        self.iam = MockIAM.from_seed(it_access.HERE / "samples/iam_seed.json", self.ledger)
        self.billing = MockBilling.from_seed(self.ledger, daily_limits={}, default_daily_limit=1000)
        self.clients: dict[str, ScopedClient] = {
            **cr_tools.tool_clients(self.clm, self.mail, self.esign),
            **it_tools.tool_clients(self.iam),
            **er_tools.tool_clients(self.billing),
        }

    def state(self) -> Any:
        """Everything any operation could have changed."""
        return (
            len(self.ledger.records()),
            self.clm.operations.records(),
            self.esign.operations.records(),
            self.iam.operations.records(),
            self.billing.operations.records(),
            self.billing.event("ev-1001"),
            self.clm.get_contract("C-1001"),
        )


ALL_SCOPES = {**cr_tools.TOOL_SCOPES, **it_tools.TOOL_SCOPES, **er_tools.TOOL_SCOPES}


def outside(client: ScopedClient) -> list[str]:
    ops = client._system.OPERATIONS  # the system's own list, not the client's
    return sorted(op for op, scope in ops.items() if scope not in client.credential.scopes)


def test_every_tool_has_a_client_with_its_own_credential(tmp_path: Path) -> None:
    s = Systems(tmp_path)
    assert set(s.clients) == set(ALL_SCOPES)
    for tool, client in s.clients.items():
        assert client.credential.principal == f"gawe-tool:{tool}"
        scopes = ALL_SCOPES[tool]
        assert client.credential.scopes == scopes


@pytest.mark.parametrize("tool", sorted(ALL_SCOPES), ids=str)
def test_a_tools_credential_is_refused_by_the_system_outside_its_scope(
    tmp_path: Path, tool: str
) -> None:
    s = Systems(tmp_path)
    client = s.clients[tool]
    before = s.state()
    for op in outside(client):
        with pytest.raises(CredentialScopeError) as refused:
            getattr(client, op)("any", "arguments", key="whatever")  # refused before they're read
        assert refused.value.code == "credential_scope"
    assert s.state() == before  # nothing was read into a change, nothing written


def test_read_tools_hold_no_scope_that_changes_anything() -> None:
    for tools in (cr_tools, it_tools, er_tools):
        for m in tools.MANIFESTS:
            if m.side_effect_class == "read" and not m.retired:
                assert all(is_read_scope(x) for x in tools.TOOL_SCOPES[m.name]), m.name


def test_a_client_offers_only_the_systems_operations(tmp_path: Path) -> None:
    s = Systems(tmp_path)
    reader = s.clients["clm.get_contract"]
    assert reader.get_contract("C-1001")["contract_id"] == "C-1001"  # its own operation works
    for internal in ("replace_contract", "contract_version", "operations", "deal_values"):
        with pytest.raises(AttributeError):
            getattr(reader, internal)


# --- startup -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"iam.lookup_user": frozenset({"user:read", "grant:write"})}, "is a read"),
        ({"iam.grant_access": frozenset()}, "lacks \\['grant:write'\\]"),
        ({"iam.grant_access": frozenset({"grant:write", "user:delete"})}, "can also change"),
    ],
)
def test_startup_refuses_a_credential_that_does_more_or_less(
    tmp_path: Path, change: dict[str, frozenset[str]], message: str
) -> None:
    bundle = it_access.bundle(tmp_path)
    with pytest.raises(ValueError, match=message):
        load_app_rules(replace(bundle, tool_scopes={**bundle.tool_scopes, **change}))


def test_startup_refuses_a_tool_without_a_credential_or_a_write_without_a_scope(
    tmp_path: Path,
) -> None:
    bundle = it_access.bundle(tmp_path)
    scopes = {k: v for k, v in bundle.tool_scopes.items() if k != "iam.grant_access"}
    with pytest.raises(ValueError, match="has no credential"):
        load_app_rules(replace(bundle, tool_scopes=scopes))
    with pytest.raises(ValueError, match="which no scope names"):
        load_app_rules(replace(bundle, write_scopes={}))


def test_both_shipped_apps_start_with_their_credentials(tmp_path: Path) -> None:
    assert load_app_rules(contract_review.bundle(tmp_path / "cr"))
    assert load_app_rules(it_access.bundle(tmp_path / "it"))
    assert load_app_rules(event_replay.bundle(tmp_path / "er"))
