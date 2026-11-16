"""A mock identity/access system for the IT-access fixture.

User records and grant operations share one SQLite database (`iam-records.sqlite3` next to the
ledger, or in memory), so "the user account still has the bound revision" and "accept this grant"
commit together. Each grant records its end date and the run (the request) it came from.
Grants from `iam.grant_access@1.0.0` stay in `iam-system.sqlite3`, read only to verify them.

Expiry, as the system's own rule: a grant is inactive at or after its `expires_at`, whether or not
anything revoked it. Revocation is a separate, idempotent record keyed by the grant, so revoking
twice records once, and a revocation that arrives before a delayed grant still applies to it.
Grants from before M16b have no end date and never expire here.
"""

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from gawe.connectors.base import FaultInjector, Ledger
from gawe.connectors.mock_operations import MockDatabase, MockOperations, operations_table
from gawe.connectors.mock_records import VersionedRecords, records_table

GrantState = Literal["absent", "active", "expired", "revoked"]
_REVOCATIONS = (
    "CREATE TABLE IF NOT EXISTS grant_revocations (grant_key TEXT PRIMARY KEY, "
    "revoke_key TEXT NOT NULL, revoked_at TEXT NOT NULL)"
)


def _end(record: Mapping[str, Any]) -> datetime | None:
    raw = record.get("expires_at")
    return datetime.fromisoformat(raw) if isinstance(raw, str) else None


class MockIAM:
    CONDITIONAL_WRITES = frozenset({("iam.grant_access", "2.0.0")})

    def __init__(self, users: Mapping[str, Mapping[str, Any]], ledger: Ledger) -> None:
        path = ledger.path.parent / "iam-records.sqlite3" if ledger.path else None
        self.database = MockDatabase(
            path,
            (records_table("users"), operations_table("grant_operations"), _REVOCATIONS),
        )
        self.users = VersionedRecords(
            self.database, "users", missing="user_not_found", normalize=str.lower
        )
        self.users.seed(users.items())
        self._ledger = ledger
        self.faults = FaultInjector()
        self.operations = MockOperations(
            ledger, "iam", database=self.database, table="grant_operations"
        )
        self.legacy_operations = MockOperations(ledger, "iam")
        # Test hook: runs after a write's separate pre-check read, before its guarded write.
        self.before_guarded_write: Callable[[], None] | None = None
        # Test hooks: a revoke that silently does nothing; an expiry rule that is not applied.
        self.ignore_revokes = False
        self.ignore_expiry = False

    @classmethod
    def from_seed(cls, seed_file: Path, ledger: Ledger) -> "MockIAM":
        data = json.loads(seed_file.read_text(encoding="utf-8"))
        return cls({u["email"]: u for u in data["users"]}, ledger)

    def lookup_user(self, email: str) -> dict[str, Any]:
        self.faults.check()
        record, _ = self.users.get(email)
        return record

    def user_version(self, email: str) -> str:
        """The account's current revision, for trusted preparation (no fault injection)."""
        _, revision = self.users.get(email)
        return revision

    def replace_user(self, email: str, fields: Mapping[str, Any]) -> str:
        """Test hook: another actor changes the account. Returns the new revision."""
        return self.users.replace(email, fields)

    def grant_access(
        self,
        key: str,
        email: str,
        system: str,
        level: str,
        *,
        user_version: str,
        expires_at: str,
        lifetime_hours: int,
        run_id: str,
    ) -> dict[str, Any]:
        self.faults.check()
        self.lookup_user(email)  # a separate read; the guard below is what decides
        if self.before_guarded_write is not None:
            self.before_guarded_write()
        self.operations.accept(
            key,
            {
                "email": email,
                "system": system,
                "level": level,
                "record": {"user_version": user_version},
                "expires_at": expires_at,
                "lifetime_hours": lifetime_hours,
                "run_id": run_id,
                "grant_id": f"gr-{key[:12]}",
            },
            guard=self.users.guard(email, user_version),
        )
        record, _ = self._ledger.record_once(
            "iam.grant", key, lambda: {"email": email, "system": system, "level": level}
        )
        data = record["data"]
        return self.operations.reply(
            {
                "grant_id": f"gr-{key[:12]}",
                "email": data["email"],
                "system": data["system"],
                "level": data["level"],
            }
        )

    # -- expiry and revocation ----------------------------------------------------------------

    def revoke(self, revoke_key: str, grant_key: str, now: datetime) -> None:
        """Revoke a grant by its operation key. Idempotent; the first revocation is kept."""
        if self.ignore_revokes:
            return

        def save(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO grant_revocations VALUES (?, ?, ?)",
                (grant_key, revoke_key, now.isoformat()),
            )

        self.database.transaction(save)

    def grant_state(self, grant_key: str, now: datetime) -> GrantState:
        """What the system says about one grant now, read from its own records."""

        def read(conn: sqlite3.Connection) -> tuple[str | None, bool]:
            row = conn.execute(
                "SELECT record FROM grant_operations WHERE operation_key=?", (grant_key,)
            ).fetchone()
            revoked = conn.execute(
                "SELECT 1 FROM grant_revocations WHERE grant_key=?", (grant_key,)
            ).fetchone()
            return (row[0] if row else None), revoked is not None

        record, revoked = self.database.transaction(read)
        if record is None:
            return "absent"
        if revoked:
            return "revoked"
        end = _end(json.loads(record))
        if end is not None and now >= end and not self.ignore_expiry:
            return "expired"
        return "active"

    def revocations(self) -> list[dict[str, str]]:
        def read(conn: sqlite3.Connection) -> list[dict[str, str]]:
            rows = conn.execute(
                "SELECT grant_key, revoke_key, revoked_at FROM grant_revocations ORDER BY rowid"
            )
            return [{"grant_key": g, "revoke_key": r, "revoked_at": t} for g, r, t in rows]

        result: list[dict[str, str]] = self.database.transaction(read)
        return result

    def overdue(self, now: datetime) -> int:
        """Grants past their end date that nothing has revoked. The system already treats them
        as inactive; a count above zero means the revocation layer is behind or broken."""

        def read(conn: sqlite3.Connection) -> int:
            count = 0
            for key, record in conn.execute("SELECT operation_key, record FROM grant_operations"):
                end = _end(json.loads(record))
                if end is None or now < end:
                    continue
                revoked = conn.execute(
                    "SELECT 1 FROM grant_revocations WHERE grant_key=?", (key,)
                ).fetchone()
                count += revoked is None
            return count

        if not self.database.exists():
            return 0
        result: int = self.database.transaction(read)
        return result
