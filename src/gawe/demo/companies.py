"""The demo's three imaginary companies and their people (M29).

All three run the same contract review app; only their settings differ (`settings.json`, which
`make demo` passes as `GAWE_TENANT_SETTINGS`). Each has a case manager who starts runs, a legal
reviewer and a finance approver. Names, addresses and tenant ids are invented.
"""

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gawe.api.auth import hash_token
from gawe.apps import contract_review
from gawe.core.tenant_settings import TenantSettings, read_settings_file

SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"


@dataclass(frozen=True)
class Person:
    key: str  # unique across the demo, e.g. "careful-alice"
    name: str
    title: str
    user_id: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class Company:
    key: str
    name: str
    tenant_id: str
    summary: str  # one line on what its settings do
    people: tuple[Person, ...]

    def person(self, role: str) -> Person:
        return next(p for p in self.people if role in p.roles)


def _people(company: str, n: int) -> tuple[Person, ...]:
    base = f"00000000-0000-0000-0000-0000000c{n}"  # each id ends in three more hex digits
    return (
        Person(f"{company}-alice", "Alice", "case manager", f"{base}0a1", ("case_manager",)),
        Person(f"{company}-bob", "Bob", "legal reviewer", f"{base}0b1", ("legal_reviewer",)),
        Person(
            f"{company}-fiona", "Fiona", "finance approver", f"{base}0f1", ("finance_approver",)
        ),
    )


COMPANIES: tuple[Company, ...] = (
    Company(
        "careful",
        "Careful Co.",
        "00000000-0000-0000-0000-0000000c0001",
        "Every low rating needs legal; a low rating on a deal over 100,000 says so.",
        _people("careful", 1),
    ),
    Company(
        "big-deals",
        "Big-Deals Co.",
        "00000000-0000-0000-0000-0000000c0002",
        "Legal only for a low rating on a deal over 250,000; other low ratings go through.",
        _people("big-deals", 2),
    ),
    Company(
        "rivals",
        "Rivals Co.",
        "00000000-0000-0000-0000-0000000c0003",
        "Every low rating needs legal, and email to a competitor's domain is blocked.",
        _people("rivals", 3),
    ),
)
BY_KEY: Mapping[str, Company] = {c.key: c for c in COMPANIES}


def settings() -> TenantSettings:
    """The companies' contract review settings, checked as the API checks them."""
    return TenantSettings(
        contract_review.SETTINGS_SCHEMA,
        contract_review.DEFAULT_SETTINGS,
        read_settings_file(SETTINGS_FILE),
    )


def write_tokens(directory: Path) -> dict[str, str]:
    """Make a fresh token for each person and write the two files the demo needs: the API's
    token file (hashes only) and the UI server's sign-in file (person key -> token). Both live
    in `directory`, which the demo keeps out of git. Returns the sign-in file's contents."""
    directory.mkdir(parents=True, exist_ok=True)
    tokens = {p.key: secrets.token_urlsafe(32) for c in COMPANIES for p in c.people}
    api: dict[str, Any] = {
        "tokens": [
            {
                "sha256": hash_token(tokens[p.key]),
                "tenant_id": c.tenant_id,
                "user_id": p.user_id,
                "roles": list(p.roles),
            }
            for c in COMPANIES
            for p in c.people
        ]
    }
    for name, content in (("api-tokens.json", api), ("sign-in.json", {"people": tokens})):
        path = directory / name
        path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
    return tokens
