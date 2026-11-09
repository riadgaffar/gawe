"""A mock e-signature service. Envelopes are written to the ledger."""

from typing import Any

from gawe.connectors.base import FaultInjector, Ledger


class MockESign:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self.faults = FaultInjector()

    def send_for_signature(
        self, key: str, contract_id: str, signer_email: str, deal_value: float
    ) -> dict[str, Any]:
        self.faults.check()
        record, _ = self._ledger.record_once(
            "esign.envelope",
            key,
            lambda: {
                "contract_id": contract_id,
                "signer_email": signer_email,
                "deal_value": deal_value,
            },
        )
        return {"envelope_id": f"env-{key[:12]}", "contract_id": record["data"]["contract_id"]}
