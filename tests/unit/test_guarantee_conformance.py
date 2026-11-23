"""M22: every guarantee a mock connector declares is one it really provides.

Startup trusts the declarations (`AppBundle.guarantees`); this checks them against behaviour, for
every declared (tool, version, guarantee). A declaration with no check here fails, so a new
guarantee, or a real connector, has to bring its own.

Each check runs the app's own write and read-back code with arguments the engine prepared (a
first run through the normal flow), then writes again directly with a fresh key.
"""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_write_readback import Env

from gawe.apps import contract_review, event_replay, it_access
from gawe.connectors.base import ConnectorError, Ledger, ToolPreDispatchError
from gawe.connectors.mock_billing import MockBilling
from gawe.connectors.mock_directory import MockDirectory
from gawe.connectors.mock_mail_server import MockMailServer
from gawe.core.executor import ToolCall
from gawe.core.runs import RunRecord

SYSTEM = {
    "crm.write_assessment": "crm",
    "esign.send_for_signature": "esign",
    "iam.grant_access": "iam",
    "email.send": "mail",
    "billing.replay_events": "billing",
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


class BillingTarget:
    """The replay write (M25), with batches prepared by the app's own preparer."""

    TENANT = "t1"

    def __init__(self, path: Path, tool: str, version: str) -> None:
        self.billing = MockBilling.from_seed(
            Ledger(path / "ledger.jsonl"), daily_limits={self.TENANT: 10**9}
        )
        for event_id in ("ev-1001", "ev-1002"):  # moved into this test's tenant
            self.billing.change_event(event_id, {"tenant_id": self.TENANT})
        self.bundle = event_replay.bundle(path, billing=self.billing)
        self.now = datetime.now(UTC)
        run = RunRecord(
            run_id="r",
            tenant_id=self.TENANT,
            acting_user_id="u",
            objective="o",
            profile="p",
            created_at=self.now,
        )
        prepare = self.bundle.preparers[(tool, version)]
        self.args = dict(prepare({"event_ids": ["ev-1001"]}, reapproval=False, run=run).args)
        self.other = dict(prepare({"event_ids": ["ev-1002"]}, reapproval=False, run=run).args)
        self.read_back = self.bundle.readbacks[(tool, version)]
        self.tool = tool

    def write(self, key: str, args: Any = None) -> Any:
        impl = self.bundle.tools[self.tool]
        return impl(args or self.args, ToolCall("r", "write", 1, key))

    def effects(self, key: str) -> int:
        return sum(r.get("operation_key") == key for r in self.billing.operations.records())

    def read(self, key: str) -> Any:
        return self.read_back(key, self.args, self.now)

    def change_bound_record(self) -> None:
        self.billing.change_event("ev-1001", {"note": "changed by someone else"})


def atomic_reservation(t: Any) -> None:
    """Two batches that each fit what is left today, but not together: one is applied."""
    t.billing.daily_limits[t.TENANT] = t.args["total"] + t.other["total"] - 1
    t.billing.inside_write = lambda conn: time.sleep(0.05)
    outcomes: list[str] = []

    def go(key: str, args: Any) -> None:
        try:
            t.write(key, args)
            outcomes.append("applied")
        except ConnectorError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=go, args=a) for a in (("k1", t.args), ("k2", t.other))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["applied", "daily_limit_exceeded"]


CHECKS: dict[str, Callable[[Any], None]] = {
    "idempotent_by_key": idempotent_by_key,
    "authoritative_readback": authoritative_readback,
    "conditional_write": conditional_write,
    "atomic_reservation": atomic_reservation,
}


@pytest.fixture(scope="module")
def declarations(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, str, str]]:
    path = tmp_path_factory.mktemp("decl")
    found: list[tuple[str, str, str]] = []
    for app in (contract_review, it_access, event_replay):
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
    ("billing.replay_events", "1.0.0", "idempotent_by_key"),
    ("billing.replay_events", "1.0.0", "authoritative_readback"),
    ("billing.replay_events", "1.0.0", "conditional_write"),
    ("billing.replay_events", "1.0.0", "atomic_reservation"),
]


def test_the_cases_are_exactly_the_declarations(declarations: list[tuple[str, str, str]]) -> None:
    assert sorted(CASES) == declarations


@pytest.mark.parametrize(("tool", "version", "guarantee"), CASES, ids=lambda x: str(x))
def test_the_mock_does_what_it_declares(
    tmp_path: Path, tool: str, version: str, guarantee: str
) -> None:
    target = BillingTarget if tool == "billing.replay_events" else Target
    CHECKS[guarantee](target(tmp_path, tool, version))
