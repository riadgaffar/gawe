import pytest

pytest.importorskip("sqlalchemy")

from gawe.db.session import (  # noqa: E402
    APP_ROLE,
    DatabaseConfigError,
    app_database_url,
    database_url,
)

ENV = (
    "GAWE_DATABASE_URL",
    "GAWE_DB_PASSWORD",
    "GAWE_DB_USER",
    "GAWE_APP_DATABASE_URL",
    "GAWE_APP_DB_PASSWORD",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_the_app_connects_as_the_limited_role_when_its_password_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAWE_DB_PASSWORD", "owner-secret")
    monkeypatch.setenv("GAWE_APP_DB_PASSWORD", "app-secret")
    url, limited = app_database_url()
    assert limited and getattr(url, "username", None) == APP_ROLE
    assert getattr(url, "password", None) == "app-secret"
    assert getattr(database_url(), "password", None) == "owner-secret"  # migrations still use it


def test_without_an_app_password_it_falls_back_to_the_owner_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAWE_DB_PASSWORD", "owner-secret")
    url, limited = app_database_url()
    assert not limited and getattr(url, "username", None) == "gawe"


def test_an_explicit_app_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAWE_DB_PASSWORD", "owner-secret")
    monkeypatch.setenv("GAWE_APP_DATABASE_URL", "postgresql+psycopg://gawe_app:x@db/gawe")
    assert app_database_url() == ("postgresql+psycopg://gawe_app:x@db/gawe", True)


def test_nothing_configured_is_an_error() -> None:
    with pytest.raises(DatabaseConfigError):
        app_database_url()
