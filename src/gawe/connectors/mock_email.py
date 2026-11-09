"""A mock email system. Nothing is sent anywhere; messages are written to the ledger."""

from typing import Any

from gawe.connectors.base import FaultInjector, Ledger


class MockEmail:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self.faults = FaultInjector()

    def send(self, key: str, to: str, subject: str, body: str) -> dict[str, Any]:
        self.faults.check()
        record, _ = self._ledger.record_once(
            "email.sent", key, lambda: {"to": to, "subject": subject, "body": body}
        )
        return {"message_id": f"msg-{key[:12]}", "to": record["data"]["to"]}
