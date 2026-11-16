"""A mock contract-lifecycle-management system plus CRM, backed by synthetic seed data.

Contract records, CRM assessment operations and e-sign envelope operations live in one SQLite
database (`clm-records.sqlite3` next to the ledger, or in memory without a ledger path), so a
write's "the contract still has the bound revision" check and its acceptance commit together.
Assessments written by `crm.write_assessment@1.0.0` stay in the older `crm-system.sqlite3`,
read only to verify writes dispatched before record versions existed.
"""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from gawe.connectors.base import ConnectorError, FaultInjector, Ledger
from gawe.connectors.mock_operations import MockDatabase, MockOperations, operations_table
from gawe.connectors.mock_records import VersionedRecords, records_table


class MockCLM:
    # Tool versions whose writes this system guards with the bound contract revision.
    CONDITIONAL_WRITES = frozenset({("crm.write_assessment", "2.0.0")})

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
        return {**record, **self.output_overrides.get(contract_id, {})}

    def contract_version(self, contract_id: str) -> str:
        """The contract's current revision, for trusted preparation (no fault injection)."""
        _, revision = self.contracts.get(contract_id)
        return revision

    def replace_contract(self, contract_id: str, fields: Mapping[str, Any]) -> str:
        """Test hook: another actor changes the contract. Returns the new revision."""
        return self.contracts.replace(contract_id, fields)

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


def deal_value_check(deal_value: float) -> Callable[[dict[str, Any]], None]:
    def check(record: dict[str, Any]) -> None:
        if float(record["deal_value"]) != float(deal_value):
            raise ConnectorError("deal_value_mismatch")

    return check
