"""M29 S5: `make demo`'s launcher, `python -m gawe.demo`.

The servers themselves are tried by hand (docs/m29-plan.md, S5); these tests check what the
launcher hands them, what stops it before it starts anything, and where token files are written.
"""

import io
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gawe.demo import __main__ as launcher
from gawe.demo.companies import COMPANIES, SETTINGS_FILE


def test_the_api_gets_the_scripted_model_the_settings_and_the_demo_tokens(tmp_path: Path) -> None:
    env = launcher.api_env(
        {"PATH": "/bin"}, store="memory", tokens_dir=tmp_path, systems_dir=tmp_path / "s"
    )
    assert env["GAWE_LLM_PROVIDER"] == "demo"
    assert env["GAWE_STORE"] == "memory"
    assert env["GAWE_APP"] == "gawe.apps.contract_review:bundle"
    assert env["GAWE_TENANT_SETTINGS"] == str(SETTINGS_FILE)
    assert env["GAWE_API_TOKENS_FILE"] == str(tmp_path / "api-tokens.json")
    assert env["GAWE_DEMO_DIR"] == str(tmp_path / "s")
    assert re.fullmatch(r"[0-9a-f]{64}", env["GAWE_POLICY_SIGNING_KEY"])
    assert env["PATH"] == "/bin"


def test_a_signing_key_already_set_is_kept(tmp_path: Path) -> None:
    env = launcher.api_env(
        {"GAWE_POLICY_SIGNING_KEY": "k" * 64},
        store="postgres",
        tokens_dir=tmp_path,
        systems_dir=tmp_path,
    )
    assert env["GAWE_POLICY_SIGNING_KEY"] == "k" * 64


def test_the_model_setting_from_env_is_overridden(tmp_path: Path) -> None:
    env = launcher.api_env(
        {"GAWE_LLM_PROVIDER": "anthropic", "GAWE_STORE": "postgres"},
        store="memory",
        tokens_dir=tmp_path,
        systems_dir=tmp_path,
    )
    assert (env["GAWE_LLM_PROVIDER"], env["GAWE_STORE"]) == ("demo", "memory")


def test_the_ui_gets_the_demo_flag_and_the_local_api(tmp_path: Path) -> None:
    env = launcher.ui_env({}, tokens_dir=tmp_path)
    assert env == {
        "GAWE_UI_DEMO": "1",
        "GAWE_UI_DEMO_SIGNIN": str(tmp_path / "sign-in.json"),
        "GAWE_API_URL": "http://127.0.0.1:8000",
    }


def test_both_servers_listen_on_the_loopback_address_only() -> None:
    assert launcher.HOST == "127.0.0.1"


def test_problems_stop_the_run_before_anything_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)

    def no_start(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("started a process")

    monkeypatch.setattr("subprocess.Popen", no_start)
    monkeypatch.setattr(launcher, "write_tokens", no_start)
    out = io.StringIO()
    assert launcher.run("memory", out) == 2
    assert "Node.js" in out.getvalue()


def test_a_busy_port_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "_port_free", lambda port: port != 3000)
    found = launcher.problems("memory", {})
    assert any("port 3000 is in use" in p for p in found)


def test_a_postgres_demo_needs_the_env_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "_port_free", lambda _port: True)
    assert any("signing key" in p for p in launcher.problems("postgres", {}))
    assert not any(
        "signing key" in p for p in launcher.problems("postgres", {"GAWE_POLICY_SIGNING_KEY": "x"})
    )


def test_seed_sql_has_every_company_and_person() -> None:
    sql = launcher.seed_sql()
    assert sql.startswith("BEGIN;") and sql.rstrip().endswith("COMMIT;")
    for company in COMPANIES:
        assert f"('{company.tenant_id}', '{company.name}')" in sql
        for person in company.people:
            assert f"'{person.user_id}', '{company.tenant_id}'" in sql
    assert sql.count("ON CONFLICT DO NOTHING") == len(COMPANIES) + sum(
        len(c.people) for c in COMPANIES
    )


def test_seed_sql_values_need_no_quoting() -> None:
    # The SQL is built from the demo's own fixed names; none may carry a quote.
    for company in COMPANIES:
        assert "'" not in company.name
        for person in company.people:
            assert "'" not in f"{person.key}{person.name}{person.title}"


def test_prepare_writes_both_token_files(tmp_path: Path) -> None:
    out = io.StringIO()
    assert launcher.main(["prepare", str(tmp_path)], out) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["api-tokens.json", "sign-in.json"]
    assert len(json.loads((tmp_path / "sign-in.json").read_text())["people"]) == 9


def test_the_demo_token_folder_is_kept_out_of_git() -> None:
    if not (launcher.ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    target = launcher.DEMO_DIR / "tokens" / "sign-in.json"
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(target)], cwd=launcher.ROOT, check=False
    )
    assert result.returncode == 0


def test_the_api_never_writes_token_files_even_in_demo_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gawe.api.main import create_app_from_env
    from gawe.demo import companies

    companies.write_tokens(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the API wrote token files")

    monkeypatch.setattr(companies, "write_tokens", refuse)
    monkeypatch.setattr(launcher, "write_tokens", refuse)
    for name, value in launcher.api_env(
        {}, store="memory", tokens_dir=tmp_path, systems_dir=tmp_path / "systems"
    ).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GAWE_MAINTENANCE_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("GAWE_VERIFICATION_DEADLINE_SECONDS", "600")
    create_app_from_env()
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert after == before


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit):
        launcher.main(["serve"], io.StringIO())


def test_the_demo_api_starts_without_the_anthropic_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `make install` leaves out the `llm` extra; the demo must not need it.
    import sys

    from gawe.api.main import create_app_from_env
    from gawe.demo import companies

    companies.write_tokens(tmp_path)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    for name, value in launcher.api_env(
        {}, store="memory", tokens_dir=tmp_path, systems_dir=tmp_path / "systems"
    ).items():
        monkeypatch.setenv(name, value)
    assert create_app_from_env() is not None
