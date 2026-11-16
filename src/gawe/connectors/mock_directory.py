"""Snapshot expansion for a mock directory; all mutation is privileged test/admin setup."""

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from gawe.connectors.base import ConnectorError
from gawe.connectors.mock_mail_state import MockMailState
from gawe.core.models import FrozenModel
from gawe.core.policy.rules import email_domain

MAX_DEPTH = 16
MAX_MAILBOXES = 100


def mailbox_identity(address: str) -> str:
    domain = email_domain(address)
    if domain is None:
        raise ConnectorError("directory_invalid_address")
    return address.split("@", 1)[0] + "@" + domain


class DirectoryEntry(FrozenModel):
    kind: Literal["mailbox", "alias", "group", "external_list", "unreadable_group"]
    members: tuple[str, ...] = ()

    @model_validator(mode="after")
    def shape(self) -> "DirectoryEntry":
        if self.kind == "alias" and len(self.members) != 1:
            raise ValueError("an alias must have one target")
        if self.kind not in {"alias", "group"} and self.members:
            raise ValueError("only aliases and groups have readable members")
        return self


class Expansion(FrozenModel):
    requested_to: str
    mailboxes: tuple[str, ...]
    directory_marker: str
    lookup_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


class MockDirectory:
    def __init__(
        self,
        directory: Path,
        *,
        entries: Mapping[str, DirectoryEntry] | None = None,
        internal_domains: tuple[str, ...] = ("acme.example",),
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.state = MockMailState(directory)
        self.clock = clock
        domains = sorted(
            {mailbox_identity("probe@" + d).split("@", 1)[1] for d in internal_domains}
        )
        with self.state.transaction() as conn:
            existing = conn.execute("SELECT value FROM metadata WHERE name='domains'").fetchone()
            if existing is None:
                for name, value in (
                    ("domains", json.dumps(domains)),
                    ("revision", "1"),
                    ("identity", uuid.uuid4().hex),
                ):
                    conn.execute("INSERT INTO metadata VALUES (?, ?)", (name, value))
                normalized = {mailbox_identity(k): v for k, v in (entries or {}).items()}
                if len(normalized) != len(entries or {}):
                    raise ValueError("duplicate canonical directory entry")
                for address, entry in normalized.items():
                    conn.execute(
                        "INSERT INTO directory VALUES (?, ?)", (address, entry.model_dump_json())
                    )
            elif json.loads(existing[0]) != domains:
                raise ValueError("persisted directory domain configuration differs")
            # Seeds initialize once; reconstructing an adapter never overwrites live membership.

    @classmethod
    def from_seed(cls, directory: Path, seed: Path) -> "MockDirectory":
        data = json.loads(seed.read_text(encoding="utf-8"))
        return cls(
            directory,
            entries={k: DirectoryEntry.model_validate(v) for k, v in data["entries"].items()},
        )

    def put(self, address: str, entry: DirectoryEntry) -> None:
        address = mailbox_identity(address)
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO directory VALUES (?, ?)", (address, entry.model_dump_json())
            )
            conn.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE name='revision'")

    def expand(self, address: str) -> Expansion:
        with self.state.transaction() as conn:
            return self.expand_in(conn, address)

    def expand_in(self, conn: sqlite3.Connection, address: str) -> Expansion:
        """Use the caller's existing transaction for guarded mock-server submission."""
        metadata = dict(conn.execute("SELECT name, value FROM metadata"))
        entries = {
            key: DirectoryEntry.model_validate_json(value)
            for key, value in conn.execute("SELECT address, entry FROM directory")
        }
        domains = set(json.loads(metadata["domains"]))
        terminals: set[str] = set()
        visiting: set[str] = set()
        # Cache by (identity, remaining depth), so shared groups cannot cause exponential work
        # and an earlier shallow visit cannot hide an excessive-depth path.
        visited: set[tuple[str, int]] = set()

        def visit(target: str, depth: int) -> None:
            target = mailbox_identity(target)
            if depth > MAX_DEPTH:
                raise ConnectorError("directory_depth_exceeded")
            if target in visiting:
                raise ConnectorError("directory_cycle")
            if (target, depth) in visited:
                return
            entry = entries.get(target)
            if entry is None:
                if target.split("@", 1)[1] in domains:
                    raise ConnectorError("directory_unknown_internal")
                terminals.add(target)
            elif entry.kind == "unreadable_group":
                raise ConnectorError("directory_unreadable")
            elif entry.kind in {"group", "alias"}:
                if not entry.members:
                    raise ConnectorError("directory_empty_group")
                visiting.add(target)
                for member in entry.members:
                    visit(member, depth + 1)
                visiting.remove(target)
            else:
                if entry.kind == "external_list" and target.split("@", 1)[1] in domains:
                    raise ConnectorError("directory_invalid_external_list")
                terminals.add(target)
            if len(terminals) > MAX_MAILBOXES:
                raise ConnectorError("directory_size_exceeded")
            visited.add((target, depth))

        visit(address, 0)
        return Expansion(
            requested_to=mailbox_identity(address),
            mailboxes=tuple(sorted(terminals)),
            directory_marker=f"{metadata['identity']}:{metadata['revision']}",
            lookup_at=self.clock(),
        )
