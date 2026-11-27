"""A mock contract-lifecycle-management system plus CRM, backed by synthetic seed data.

Contract records, CRM assessment operations and e-sign envelope operations live in one SQLite
database (`clm-records.sqlite3` next to the ledger, or in memory without a ledger path), so a
write's "the contract still has the bound revision" check and its acceptance commit together.
Assessments written by `crm.write_assessment@1.0.0` stay in the older `crm-system.sqlite3`,
read only to verify writes dispatched before record versions existed.

Since M26 the contract also holds its current rating (`risk_level`, `risk_summary`). A
`set_assessment` (3.0.0) or `restore_assessment` changes them in the guarded transaction and
issues a new revision, so the system's own writes move the revision like anyone else's, and an
undo bound to the revision after a write refuses once anything changed since. 2.0.0 still only
appends an operation and leaves the contract as it was.
"""

import json
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from gawe.connectors.base import ConnectorError, FaultInjector, Ledger
from gawe.connectors.credentials import Credential, authorize
from gawe.connectors.mock_operations import MockDatabase, MockOperations, operations_table
from gawe.connectors.mock_records import VersionedRecords, records_table
from gawe.core.guarantees import EVERY_WRITE


class MockCLM:
    # What a tool's credential must hold for each operation (M23). The system checks it first.
    OPERATIONS: ClassVar[Mapping[str, str]] = {
        "get_contract": "contract:read",
        "write_assessment": "assessment:write",
        "set_assessment": "assessment:write",
        "restore_assessment": "assessment:restore",
    }
    # The contract's current rating (M26). get_contract leaves these out: what a tool reads about
    # a contract is unchanged, and the rating reaches GAWE only through trusted preparation.
    RATING_FIELDS: ClassVar[tuple[str, str]] = ("risk_level", "risk_summary")

    def authorize(self, credential: Credential, operation: str) -> None:
        authorize(self, credential, operation)

    # What this system guarantees for its writes (M22): each operation once per GAWE's key, and its
    # own record of it, read by that key. The current version also checks the bound record's
    # revision in the write's transaction; 1.0.0 (the older store) doesn't. 3.0.0 (M26) can also
    # be reversed: `restore_assessment` at the revision the write left, read back by its key.
    GUARANTEES = {
        ("crm.restore_assessment", "1.0.0"): EVERY_WRITE | {"conditional_write"},
        ("crm.restore_assessment", "2.0.0"): EVERY_WRITE | {"conditional_write"},
        ("crm.write_assessment", "3.0.0"): EVERY_WRITE | {"conditional_write", "reversible"},
        ("crm.write_assessment", "4.0.0"): EVERY_WRITE | {"conditional_write", "reversible"},
        ("crm.write_assessment", "2.0.0"): EVERY_WRITE | {"conditional_write"},
        ("crm.write_assessment", "1.0.0"): EVERY_WRITE,
    }

    def __init__(self, contracts: Mapping[str, Mapping[str, Any]], ledger: Ledger) -> None:
        path = ledger.path.parent / "clm-records.sqlite3" if ledger.path else None
        self.database = MockDatabase(
            path,
            (
                records_table("contracts"),
                operations_table("crm_operations"),
                operations_table("esign_operations"),
            ),
        )
        self.contracts = VersionedRecords(
            self.database, "contracts", missing="contract_not_found", normalize=str
        )
        self.contracts.seed(contracts.items())
        self._ledger = ledger
        self.faults = FaultInjector()
        self.operations = MockOperations(
            ledger, "crm", database=self.database, table="crm_operations"
        )
        self.legacy_operations = MockOperations(ledger, "crm")
        # Test fault hook: fields returned by get_contract in place of the seed's (poisoned output).
        # It never changes deal_values(), which policy reads from the system of record.
        self.output_overrides: dict[str, dict[str, Any]] = {}
        # Test hook: runs after a write's separate pre-check read, before its guarded write.
        self.before_guarded_write: Callable[[], None] | None = None

    @classmethod
    def from_seed(cls, seed_file: Path, ledger: Ledger) -> "MockCLM":
        data = json.loads(seed_file.read_text(encoding="utf-8"))
        return cls({c["contract_id"]: c for c in data["contracts"]}, ledger)

    def get_contract(self, contract_id: str) -> dict[str, Any]:
        self.faults.check()
        record, _ = self.contracts.get(contract_id)
        contract = {k: v for k, v in record.items() if k not in self.RATING_FIELDS}
        return {**contract, **self.output_overrides.get(contract_id, {})}

    def current_contract(self, contract_id: str) -> tuple[dict[str, Any], str]:
        """Trusted full record and revision from one read; no model-output overrides."""
        return self.contracts.get(contract_id)

    def current_assessment(self, contract_id: str) -> tuple[dict[str, str] | None, str]:
        """(current rating or None, revision) from one read, for trusted preparation: the
        rating is exactly the one at that revision (no fault injection)."""
        record, revision = self.contracts.get(contract_id)
        if "risk_level" not in record:
            return None, revision
        return {"risk_level": record["risk_level"], "summary": record["risk_summary"]}, revision

    def contract_version(self, contract_id: str) -> str:
        """The contract's current revision, for trusted preparation (no fault injection)."""
        _, revision = self.contracts.get(contract_id)
        return revision

    def replace_contract(
        self, contract_id: str, fields: Mapping[str, Any], *, remove: tuple[str, ...] = ()
    ) -> str:
        """Test hook: another actor changes the contract. Returns the new revision."""
        return self.contracts.replace(contract_id, fields, remove=remove)

    def deal_values(self) -> dict[str, float]:
        """Every contract's deal value, from the contract system (never from a document)."""
        return {cid: float(c["deal_value"]) for cid, c in self.contracts.all().items()}

    def fire_before_guarded_write(self) -> None:
        if self.before_guarded_write is not None:
            self.before_guarded_write()

    def write_assessment(
        self,
        key: str,
        contract_id: str,
        risk_level: str,
        summary: str,
        *,
        contract_version: str,
    ) -> dict[str, Any]:
        self.faults.check()
        self.get_contract(contract_id)  # a separate read; the guard below is what decides
        self.fire_before_guarded_write()
        self.operations.accept(
            key,
            {
                "contract_id": contract_id,
                "risk_level": risk_level,
                "summary": summary,
                "record": {"contract_version": contract_version},
                "assessment_id": f"as-{key[:12]}",
            },
            guard=self.contracts.guard(contract_id, contract_version),
        )
        record, _ = self._ledger.record_once(
            "crm.assessment",
            key,
            lambda: {"contract_id": contract_id, "risk_level": risk_level, "summary": summary},
        )
        return self.operations.reply(
            {
                "assessment_id": f"as-{key[:12]}",
                "contract_id": record["data"]["contract_id"],
                "risk_level": record["data"]["risk_level"],
            }
        )

    def _change_rating(
        self, contract_id: str, rating: Mapping[str, str] | None
    ) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """The effect of a rating write, run after the revision guard in its transaction."""

        def effect(conn: sqlite3.Connection) -> dict[str, Any]:
            if rating is None:
                after = self.contracts.update_in(
                    conn, contract_id, set_fields={}, remove_fields=self.RATING_FIELDS
                )
            else:
                after = self.contracts.update_in(
                    conn,
                    contract_id,
                    set_fields={
                        "risk_level": rating["risk_level"],
                        "risk_summary": rating["summary"],
                    },
                )
            return {"result_version": after}

        return effect

    def set_assessment(
        self,
        key: str,
        contract_id: str,
        risk_level: str,
        summary: str,
        *,
        contract_version: str,
    ) -> dict[str, Any]:
        """crm.write_assessment@3.0.0: set the contract's current rating while it still has the
        bound revision, and issue a new one. The operation records both revisions."""
        self.faults.check()
        self.get_contract(contract_id)  # a separate read; the guard below is what decides
        self.fire_before_guarded_write()
        rating = {"risk_level": risk_level, "summary": summary}
        data = self.operations.accept(
            key,
            {
                "contract_id": contract_id,
                "risk_level": risk_level,
                "summary": summary,
                "record": {"contract_version": contract_version},
                "assessment_id": f"as-{key[:12]}",
            },
            guard=self.contracts.guard(contract_id, contract_version),
            effect=self._change_rating(contract_id, rating),
        )
        self._ledger.record_once(
            "crm.assessment", key, lambda: {**rating, "contract_id": contract_id}
        )
        return self.operations.reply(
            {
                "assessment_id": data["assessment_id"],
                "contract_id": data["contract_id"],
                "risk_level": data["risk_level"],
            }
        )

    def restore_assessment(
        self,
        key: str,
        contract_id: str,
        prior: Mapping[str, str] | None,
        *,
        contract_version: str,
    ) -> dict[str, Any]:
        """crm.restore_assessment@1.0.0: put back the rating a write replaced (`prior`, or no
        rating at all) only while the contract has `contract_version`, the revision recorded
        after that write. Any later change, including one that changed it back, moved the
        revision, so the restore is refused with `record_changed` and nothing is written."""
        self.faults.check()
        self.fire_before_guarded_write()
        data = self.operations.accept(
            key,
            {
                "contract_id": contract_id,
                "restored": dict(prior) if prior is not None else None,
                "record": {"contract_version": contract_version},
                "restore_id": f"rs-{key[:12]}",
            },
            guard=self.contracts.guard(contract_id, contract_version),
            effect=self._change_rating(contract_id, prior),
        )
        self._ledger.record_once(
            "crm.restore",
            key,
            lambda: {"contract_id": contract_id, "restored": data["restored"]},
        )
        return self.operations.reply(
            {
                "restore_id": data["restore_id"],
                "contract_id": data["contract_id"],
                "restored_level": (data["restored"] or {}).get("risk_level"),
            }
        )


def deal_value_check(deal_value: float) -> Callable[[dict[str, Any]], None]:
    def check(record: dict[str, Any]) -> None:
        if float(record["deal_value"]) != float(deal_value):
            raise ConnectorError("deal_value_mismatch")

    return check
