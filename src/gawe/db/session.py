"""Database connection settings. Everything comes from the environment; nothing is hardcoded.

Either set GAWE_DATABASE_URL (a full SQLAlchemy URL, e.g. postgresql+psycopg://...), or set
GAWE_DB_PASSWORD plus optionally GAWE_DB_USER / GAWE_DB_NAME / GAWE_DB_HOST / GAWE_DB_PORT.
"""

import os
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

DRIVER = "postgresql+psycopg"


class DatabaseConfigError(RuntimeError):
    """Required database settings are missing or invalid."""


APP_ROLE = "gawe_app"


def _url(username: str, password: str) -> URL:
    raw_port = os.environ.get("GAWE_DB_PORT", "5432")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise DatabaseConfigError(f"GAWE_DB_PORT must be an integer, got {raw_port!r}") from exc
    # URL.create escapes the password, so special characters are safe.
    return URL.create(
        DRIVER,
        username=username,
        password=password,
        host=os.environ.get("GAWE_DB_HOST", "localhost"),
        port=port,
        database=os.environ.get("GAWE_DB_NAME", "gawe"),
    )


def database_url() -> str | URL:
    """The owner connection: used for migrations and tests."""
    explicit = os.environ.get("GAWE_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    password = os.environ.get("GAWE_DB_PASSWORD", "")
    if not password:
        raise DatabaseConfigError(
            "set GAWE_DATABASE_URL, or GAWE_DB_PASSWORD (see .env.example; `make env` creates it)"
        )
    return _url(os.environ.get("GAWE_DB_USER", "gawe"), password)


def app_database_url() -> tuple[str | URL, bool]:
    """The connection for the running API, and whether it is the least-privilege role.

    GAWE_APP_DATABASE_URL, or GAWE_APP_DB_PASSWORD (connects as `gawe_app`), selects the limited
    role created by migration 0004 and `make app-role`. With neither set this falls back to the
    owner connection so a fresh checkout still runs, and the second value is False so the caller
    can warn.
    """
    explicit = os.environ.get("GAWE_APP_DATABASE_URL", "").strip()
    if explicit:
        return explicit, True
    password = os.environ.get("GAWE_APP_DB_PASSWORD", "")
    if password:
        return _url(APP_ROLE, password), True
    return database_url(), False


def make_engine(url: str | URL | None = None, **kwargs: Any) -> Engine:
    """Create an engine. Keep the default READ COMMITTED isolation: the audit store's retry
    logic relies on each statement seeing the latest committed chain head."""
    return create_engine(url if url is not None else database_url(), pool_pre_ping=True, **kwargs)
