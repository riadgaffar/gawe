"""A mock e-signature service. Envelopes are written to the ledger.

Envelope operations share the contract system's database, so "the contract still has the bound
revision and the bound deal value" and "accept this envelope" commit in one transaction, the way
a real service creates an envelope from one specific document version. Envelopes from
`esign.send_for_signature@1.0.0` stay in `esign-system.sqlite3`, read only to verify them.
"""

from collections.abc import Mapping
from typing import Any, ClassVar

from gawe.connectors.base import FaultInjector, Ledger
from gawe.connectors.credentials import Credential, authorize
from gawe.connectors.mock_clm import MockCLM, deal_value_check
from gawe.connectors.mock_operations import MockOperations
from gawe.core.guarantees import EVERY_WRITE


class MockESign:
    # What a tool's credential must hold for each operation (M23). The system checks it first.
    OPERATIONS: ClassVar[Mapping[str, str]] = {"send_for_signature": "envelope:send"}

    def authorize(self, credential: Credential, operation: str) -> None:
        authorize(self, credential, operation)

    # What this system guarantees for its writes (M22): each operation once per GAWE's key, and its
    # own record of it, read by that key. The current version also checks the bound record's
    # revision in the write's transaction; 1.0.0 (the older store) doesn't.
    GUARANTEES = {
        ("esign.send_for_signature", "2.0.0"): EVERY_WRITE | {"conditional_write"},
        ("esign.send_for_signature", "3.0.0"): EVERY_WRITE | {"conditional_write"},
        ("esign.send_for_signature", "1.0.0"): EVERY_WRITE,
    }

    def __init__(self, ledger: Ledger, clm: MockCLM) -> None:
        self._ledger = ledger
        self._clm = clm
        self.faults = FaultInjector()
        self.operations = MockOperations(
            ledger, "esign", database=clm.database, table="esign_operations"
        )
        self.legacy_operations = MockOperations(ledger, "esign")

    def send_for_signature(
        self,
        key: str,
        contract_id: str,
        signer_email: str,
        deal_value: float,
        *,
        contract_version: str,
    ) -> dict[str, Any]:
        self.faults.check()
        self._clm.fire_before_guarded_write()
        self.operations.accept(
            key,
            {
                "contract_id": contract_id,
                "signer_email": signer_email,
                "deal_value": deal_value,
                "record": {"contract_version": contract_version},
                "envelope_id": f"env-{key[:12]}",
            },
            guard=self._clm.contracts.guard(
                contract_id, contract_version, deal_value_check(deal_value)
            ),
        )
        record, _ = self._ledger.record_once(
            "esign.envelope",
            key,
            lambda: {
                "contract_id": contract_id,
                "signer_email": signer_email,
                "deal_value": deal_value,
            },
        )
        return self.operations.reply(
            {
                "envelope_id": f"env-{key[:12]}",
                "contract_id": record["data"]["contract_id"],
            }
        )
