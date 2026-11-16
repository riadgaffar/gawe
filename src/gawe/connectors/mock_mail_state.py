"""Durable state shared by the mock directory and mock mail server, not the app ledger.

BEGIN IMMEDIATE serializes directory mutation with recipient checking and acceptance across
processes. This local mock transaction does not claim atomicity for a real directory/mail API.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class MockMailState:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "mail-system.sqlite3"
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS directory "
                "(address TEXT PRIMARY KEY, entry TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS accepted "
                "(operation_key TEXT PRIMARY KEY, record TEXT NOT NULL)"
            )
            if "visible_at" not in {r[1] for r in conn.execute("PRAGMA table_info(accepted)")}:
                conn.execute("ALTER TABLE accepted ADD COLUMN visible_at TEXT")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
