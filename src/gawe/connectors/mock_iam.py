"""A mock identity/access system for the IT-access fixture."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gawe.connectors.base import ConnectorError, FaultInjector, Ledger


class MockIAM:
    def __init__(self, users: Mapping[str, Mapping[str, Any]], ledger: Ledger) -> None:
        self._users = {k.lower(): dict(v) for k, v in users.items()}
        self._ledger = ledger
        self.faults = FaultInjector()

    @classmethod
    def from_seed(cls, seed_file: Path, ledger: Ledger) -> "MockIAM":
        data = json.loads(seed_file.read_text(encoding="utf-8"))
        return cls({u["email"]: u for u in data["users"]}, ledger)

    def lookup_user(self, email: str) -> dict[str, Any]:
        self.faults.check()
        try:
            return dict(self._users[email.lower()])
        except KeyError:
            raise ConnectorError("user_not_found") from None

    def grant_access(self, key: str, email: str, system: str, level: str) -> dict[str, Any]:
        self.faults.check()
        self.lookup_user(email)
        record, _ = self._ledger.record_once(
            "iam.grant", key, lambda: {"email": email, "system": system, "level": level}
        )
        data = record["data"]
        return {
            "grant_id": f"gr-{key[:12]}",
            "email": data["email"],
            "system": data["system"],
            "level": data["level"],
        }
