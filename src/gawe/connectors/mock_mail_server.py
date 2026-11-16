"""Independent mock RCPT TO acceptance records. Nothing is delivered or sent over a network."""

import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime

from gawe.connectors.base import ConnectorError
from gawe.connectors.mock_directory import (
    MAX_MAILBOXES,
    DirectoryEntry,
    Expansion,
    MockDirectory,
    mailbox_identity,
)
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.models import FrozenModel


class AcceptedMessage(FrozenModel):
    operation_key: str
    request_digest: str
    accepted_recipients: tuple[str, ...]
    header_to: str
    subject_digest: str
    body_digest: str
    accepted_at: datetime


class MockMailServer:
    def __init__(self, directory: MockDirectory) -> None:
        self.directory = directory
        self.visible_at: datetime | None = None
        self.read_failures = 0
        self.accepted_override: tuple[str, ...] | None = None
        self.subject_override: str | None = None
        self.body_override: str | None = None

    def read(self, operation_key: str, *, now: datetime | None = None) -> AcceptedMessage | None:
        if self.read_failures:
            self.read_failures -= 1
            raise OSError("mock mail read unavailable")
        with self.directory.state.transaction() as conn:
            row = conn.execute(
                "SELECT visible_at FROM accepted WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if row and now is not None and row[0] and now < datetime.fromisoformat(row[0]):
                return None
            return self._read(conn, operation_key)

    def records(self) -> list[AcceptedMessage]:
        with self.directory.state.transaction() as conn:
            return [
                AcceptedMessage.model_validate_json(row[0])
                for row in conn.execute("SELECT record FROM accepted ORDER BY rowid")
            ]

    def _read(self, conn: sqlite3.Connection, key: str) -> AcceptedMessage | None:
        row = conn.execute("SELECT record FROM accepted WHERE operation_key=?", (key,)).fetchone()
        return AcceptedMessage.model_validate_json(row[0]) if row else None

    def submit(
        self,
        key: str,
        expansion: Expansion,
        *,
        header_to: str,
        subject: str,
        body: str,
        before_check: Callable[[], None] | None = None,
        after_check: Callable[[], None] | None = None,
    ) -> AcceptedMessage:
        """Recheck under the same transaction that accepts; hooks are test-only fault controls.

        Retry of an already accepted identical request returns evidence without another send,
        even if membership subsequently changed. A changed request cannot reuse its operation key.
        Lookup time is deliberately excluded from request identity.
        """
        if before_check:
            before_check()
        digest = sha256_hex(
            canonical_json(
                {
                    "requested_to": expansion.requested_to,
                    "mailboxes": expansion.mailboxes,
                    "marker": expansion.directory_marker,
                    "header_to": header_to,
                    "subject": subject,
                    "body": body,
                }
            )
        )
        with self.directory.state.transaction() as conn:
            saved = self._read(conn, key)
            if saved:
                if saved.request_digest != digest:
                    raise ConnectorError("mail_idempotency_conflict")
                return saved
            fresh = self.directory.expand_in(conn, expansion.requested_to)
            if (
                fresh.mailboxes != expansion.mailboxes
                or fresh.directory_marker != expansion.directory_marker
            ):
                raise ConnectorError("directory_changed")
            if after_check:
                after_check()
            return self._accept(conn, key, expansion.mailboxes, header_to, subject, body, digest)

    def accept(
        self,
        key: str,
        recipients: Sequence[str],
        *,
        header_to: str,
        subject: str,
        body: str,
    ) -> AcceptedMessage:
        """Raw mock-server boundary for tests; no group expansion, no app authorization claim.

        The application adapter must use submit(), which also checks the approved snapshot.
        """
        digest = sha256_hex(
            canonical_json(
                {
                    "recipients": list(recipients),
                    "header_to": header_to,
                    "subject": subject,
                    "body": body,
                }
            )
        )
        with self.directory.state.transaction() as conn:
            saved = self._read(conn, key)
            if saved:
                if saved.request_digest != digest:
                    raise ConnectorError("mail_idempotency_conflict")
                return saved
            return self._accept(conn, key, recipients, header_to, subject, body, digest)

    def _accept(
        self,
        conn: sqlite3.Connection,
        key: str,
        recipients: Sequence[str],
        header_to: str,
        subject: str,
        body: str,
        digest: str,
    ) -> AcceptedMessage:
        targets = self.accepted_override if self.accepted_override is not None else recipients
        if not key or not targets or len(targets) > MAX_MAILBOXES:
            raise ConnectorError("mail_invalid_envelope")
        canonical = tuple(sorted({mailbox_identity(r) for r in targets}))
        for recipient in canonical:
            resolved = self.directory.expand_in(conn, recipient)
            row = conn.execute(
                "SELECT entry FROM directory WHERE address=?", (recipient,)
            ).fetchone()
            # Even a one-member group/alias that expands back to one address is not an envelope.
            if row and DirectoryEntry.model_validate_json(row[0]).kind not in {
                "mailbox",
                "external_list",
            }:
                raise ConnectorError("mail_nonterminal_recipient")
            if resolved.mailboxes != (recipient,):
                raise ConnectorError("mail_nonterminal_recipient")
        record = AcceptedMessage(
            operation_key=key,
            request_digest=digest,
            accepted_recipients=canonical,
            header_to=header_to,
            subject_digest=sha256_hex(
                (self.subject_override if self.subject_override is not None else subject).encode()
            ),
            body_digest=sha256_hex(
                (self.body_override if self.body_override is not None else body).encode()
            ),
            accepted_at=self.directory.clock(),
        )
        conn.execute(
            "INSERT INTO accepted (operation_key, record, visible_at) VALUES (?, ?, ?)",
            (
                key,
                record.model_dump_json(),
                self.visible_at.isoformat() if self.visible_at else None,
            ),
        )
        return record
