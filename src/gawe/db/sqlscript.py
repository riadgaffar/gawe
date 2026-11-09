"""Apply a plain-SQL migration file inside the caller's transaction.

The migration files are written to run standalone under psql, so they carry their own
BEGIN/COMMIT. When Alembic (or a test fixture) already owns the transaction we strip those two
lines. The script is then sent through the raw DBAPI cursor with no parameters: that avoids
SQLAlchemy and psycopg treating `:` or `%` inside function bodies (for example the RAISE format
strings) as bind placeholders, and lets one call carry many statements.
"""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

_TRANSACTION_LINES = {"BEGIN;", "COMMIT;"}


def strip_transaction_control(sql: str) -> str:
    """Drop lines that are exactly BEGIN; or COMMIT; (the files use no other form)."""
    kept = [line for line in sql.splitlines() if line.strip() not in _TRANSACTION_LINES]
    return "\n".join(kept) + "\n"


def load_script(path: str | Path) -> str:
    return strip_transaction_control(Path(path).read_text(encoding="utf-8"))


def run_script(connection: "Connection", sql: str) -> None:
    # Make SQLAlchemy begin its transaction first, so a later connection.commit() really commits
    # what the raw cursor does below.
    connection.exec_driver_sql("SELECT 1")
    cursor = connection.connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()
