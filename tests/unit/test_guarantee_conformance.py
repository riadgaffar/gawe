"""M22: every guarantee a mock connector declares is one it really provides.

Startup trusts the declarations (`AppBundle.guarantees`); this checks them against behaviour, for
every declared (tool, version, guarantee). A declaration with no check here fails, so a new
guarantee, or a real connector, has to bring its own.

Each check runs the app's own write and read-back code with arguments the engine prepared (a
first run through the normal flow), then writes again directly with a fresh key.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_write_readback import Env

from gawe.apps import contract_review, it_access
from gawe.connectors.base import ToolPreDispatchError
from gawe.connectors.mock_directory import MockDirectory
from gawe.connectors.mock_mail_server import MockMailServer
from gawe.core.executor import ToolCall

SYSTEM = {
    "crm.write_assessment": "crm",
    "esign.send_for_signature": "esign",
    "iam.grant_access": "iam",
    "email.send": "mail",
}


class Target:
    """One write tool version in a fresh environment, with prepared arguments."""

    def __init__(self, path: Path, tool: str, version: str) -> None:
        self.tool, self.version = tool, version
        self.system = SYSTEM[tool]
        self.e = Env(path, self.system)
        self.e.advance()  # the normal flow prepares (and sends) once
        self.args = dict(self.e.stores.steps.get(self.e.run_id, "write").resolved_args)
        self.read_back = self.e.bundle.readbacks[(tool, version)]

    def write(self, key: str) -> Any:
        if self.version == "2.0.0":
            impl = self.e.bundle.tools[self.tool]
            return impl(self.args, ToolCall(self.e.run_id, "write", 1, key))
        # A 1.0.0 write went to the system's older store; there is no current code for it.
        legacy = {"crm": self.e.clm, "esign": self.e.esign, "iam": self.e.iam}[self.system]
        return legacy.legacy_operations.accept(key, self.args)

    def effects(self, key: str) -> int:
        if self.system == "mail":
            server = MockMailServer(MockDirectory(self.e.ledger_dir))
            return sum(r.operation_key == key for r in server.records())
        owner = {"crm": self.e.clm, "esign": self.e.esign, "iam": self.e.iam}[self.system]
        store = owner.operations if self.version == "2.0.0" else owner.legacy_operations
        return sum(r.get("operation_key") == key for r in store.records())

    def read(self, key: str) -> Any:
        return self.read_back(key, self.args, self.e.clock.now)

    def change_bound_record(self) -> None:
        if self.system == "iam":
            self.e.iam.replace_user(self.args["email"], {"note": "changed by someone else"})
        else:
            self.e.clm.replace_contract(self.args["contract_id"], {"note": "changed"})


def idempotent_by_key(t: Target) -> None:
    t.write("conformance-key")
    t.write("conformance-key")  # a retry after a lost reply
    assert t.effects("conformance-key") == 1


def authoritative_readback(t: Target) -> None:
    t.write("conformance-key")
    assert t.read("conformance-key").status == "agreement"
    assert t.read("never-sent").status != "agreement"  # the system's own record, by key


def conditional_write(t: Target) -> None:
    t.change_bound_record()  # the revision the arguments were prepared against is now stale
    with pytest.raises(ToolPreDispatchError):
        t.write("stale-key")
    assert t.effects("stale-key") == 0
    assert t.read("stale-key").status != "agreement"


CHECKS: dict[str, Callable[[Target], None]] = {
    "idempotent_by_key": idempotent_by_key,
    "authoritative_readback": authoritative_readback,
    "conditional_write": conditional_write,
}


@pytest.fixture(scope="module")
def declarations(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, str, str]]:
    path = tmp_path_factory.mktemp("decl")
    found: list[tuple[str, str, str]] = []
    for app in (contract_review, it_access):
        for (tool, version), guarantees in app.bundle(path / app.__name__).guarantees.items():
            found += [(tool, version, g) for g in sorted(guarantees)]
    return sorted(found)


def test_every_declared_guarantee_has_a_check(declarations: list[tuple[str, str, str]]) -> None:
    assert declarations, "the shipped apps declare guarantees"
    unchecked = sorted({g for _, _, g in declarations} - set(CHECKS))
    assert unchecked == [], f"declared with no conformance check: {unchecked}"
    assert {t for t, _, _ in declarations} <= set(SYSTEM)


CASES = [
    ("crm.write_assessment", "2.0.0", "idempotent_by_key"),
    ("crm.write_assessment", "2.0.0", "authoritative_readback"),
    ("crm.write_assessment", "2.0.0", "conditional_write"),
    ("crm.write_assessment", "1.0.0", "idempotent_by_key"),
    ("crm.write_assessment", "1.0.0", "authoritative_readback"),
    ("esign.send_for_signature", "2.0.0", "idempotent_by_key"),
    ("esign.send_for_signature", "2.0.0", "authoritative_readback"),
    ("esign.send_for_signature", "2.0.0", "conditional_write"),
    ("esign.send_for_signature", "1.0.0", "idempotent_by_key"),
    ("esign.send_for_signature", "1.0.0", "authoritative_readback"),
    ("iam.grant_access", "2.0.0", "idempotent_by_key"),
    ("iam.grant_access", "2.0.0", "authoritative_readback"),
    ("iam.grant_access", "2.0.0", "conditional_write"),
    ("iam.grant_access", "1.0.0", "idempotent_by_key"),
    ("iam.grant_access", "1.0.0", "authoritative_readback"),
    ("email.send", "2.0.0", "idempotent_by_key"),
    ("email.send", "2.0.0", "authoritative_readback"),
]


def test_the_cases_are_exactly_the_declarations(declarations: list[tuple[str, str, str]]) -> None:
    assert sorted(CASES) == declarations


@pytest.mark.parametrize(("tool", "version", "guarantee"), CASES, ids=lambda x: str(x))
def test_the_mock_does_what_it_declares(
    tmp_path: Path, tool: str, version: str, guarantee: str
) -> None:
    CHECKS[guarantee](Target(tmp_path, tool, version))
