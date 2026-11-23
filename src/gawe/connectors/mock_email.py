"""A mock email system. Nothing is sent anywhere; messages are written to the ledger."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from gawe.connectors.base import ConnectorError, FaultInjector, Ledger
from gawe.connectors.credentials import Credential, authorize
from gawe.connectors.mock_directory import Expansion, MockDirectory
from gawe.connectors.mock_mail_server import MockMailServer
from gawe.core.guarantees import EVERY_WRITE
from gawe.core.preparation import RecipientChanged


class MockEmail:
    # What a tool's credential must hold for each operation (M23). The system checks it first.
    OPERATIONS: ClassVar[Mapping[str, str]] = {"send": "mail:send", "send_prepared": "mail:send"}

    def authorize(self, credential: Credential, operation: str) -> None:
        authorize(self, credential, operation)

    # What sending guarantees (M22): one message per GAWE's key, and the mail server's own record
    # of what it accepted, read by that key. Nothing to bind, so no conditional write.
    GUARANTEES = {("email.send", "2.0.0"): EVERY_WRITE}

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self.faults = FaultInjector()
        self.server: MockMailServer | None = None
        self.reply_override: dict[str, Any] = {}
        self.drop_reply = False

    def send(self, key: str, to: str, subject: str, body: str) -> dict[str, Any]:
        self.faults.check()
        record, _ = self._ledger.record_once(
            "email.sent", key, lambda: {"to": to, "subject": subject, "body": body}
        )
        return {"message_id": f"msg-{key[:12]}", "to": record["data"]["to"]}

    def configure(self, directory: MockDirectory) -> None:
        self.server = MockMailServer(directory)

    def send_prepared(self, key: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.server is None:
            raise ConnectorError("mail_server_unconfigured")
        self.faults.check()  # known pre-dispatch failure; no acceptance possible
        delivery = args["delivery"]
        expansion = Expansion(
            requested_to=args["to"],
            mailboxes=tuple(delivery["mailboxes"]),
            directory_marker=delivery["directory_marker"],
            lookup_at=datetime.now(UTC),
        )
        try:
            accepted = self.server.submit(
                key, expansion, header_to=args["to"], subject=args["subject"], body=args["body"]
            )
        except ConnectorError as exc:
            if exc.code == "directory_changed":
                raise RecipientChanged() from exc
            raise
        self._ledger.record_once(
            "email.sent",
            key,
            lambda: {
                "to": args["to"],
                "subject": args["subject"],
                "body": args["body"],
                "accepted_recipients": list(accepted.accepted_recipients),
            },
        )
        if self.drop_reply:
            raise TimeoutError("mock response lost after acceptance")
        return {
            "message_id": f"msg-{key[:12]}",
            "to": args["to"],
            "accepted_recipients": list(accepted.accepted_recipients),
            **self.reply_override,
        }
