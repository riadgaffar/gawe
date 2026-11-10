"""A mock contract-lifecycle-management system plus CRM, backed by synthetic seed data."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gawe.connectors.base import ConnectorError, FaultInjector, Ledger


class MockCLM:
    def __init__(self, contracts: Mapping[str, Mapping[str, Any]], ledger: Ledger) -> None:
        self._contracts = {k: dict(v) for k, v in contracts.items()}
        self._ledger = ledger
        self.faults = FaultInjector()

    @classmethod
    def from_seed(cls, seed_file: Path, ledger: Ledger) -> "MockCLM":
        data = json.loads(seed_file.read_text(encoding="utf-8"))
        return cls({c["contract_id"]: c for c in data["contracts"]}, ledger)

    def get_contract(self, contract_id: str) -> dict[str, Any]:
        self.faults.check()
        try:
            return dict(self._contracts[contract_id])
        except KeyError:
            raise ConnectorError("contract_not_found") from None

    def deal_values(self) -> dict[str, float]:
        """Every contract's deal value, from the contract system (never from a document)."""
        return {cid: float(c["deal_value"]) for cid, c in self._contracts.items()}

    def write_assessment(
        self, key: str, contract_id: str, risk_level: str, summary: str
    ) -> dict[str, Any]:
        self.faults.check()
        self.get_contract(contract_id)
        record, _ = self._ledger.record_once(
            "crm.assessment",
            key,
            lambda: {"contract_id": contract_id, "risk_level": risk_level, "summary": summary},
        )
        return {
            "assessment_id": f"as-{key[:12]}",
            "contract_id": record["data"]["contract_id"],
            "risk_level": record["data"]["risk_level"],
        }
