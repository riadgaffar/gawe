"""M29 S3: the demo's companies, their people and tokens, and the pre-built tampered run."""

import json
import secrets
import stat
from pathlib import Path
from typing import Any

import pytest
from tests.helpers import signer

from gawe.api.auth import TokenAuthenticator
from gawe.api.wiring import memory_stores
from gawe.core.audit import verify_chain
from gawe.core.tenant_settings import read_settings_file
from gawe.demo import fixtures
from gawe.demo.companies import BY_KEY, COMPANIES, SETTINGS_FILE, settings, write_tokens

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

# --- companies and settings ---------------------------------------------------------------------


def test_the_settings_file_names_exactly_the_three_companies() -> None:
    assert set(read_settings_file(SETTINGS_FILE)) == {c.tenant_id for c in COMPANIES}


def test_each_companys_settings_are_as_the_design_sets_them() -> None:
    s = settings()
    careful = s.for_tenant(BY_KEY["careful"].tenant_id)
    big_deals = s.for_tenant(BY_KEY["big-deals"].tenant_id)
    rivals = s.for_tenant(BY_KEY["rivals"].tenant_id)
    assert (careful["review_levels_always"], careful["large_deal_threshold"]) == (["low"], 100_000)
    assert careful["blocked_domains"] == ["evil.example"]
    assert (big_deals["review_levels_always"], big_deals["large_deal_threshold"]) == ([], 250_000)
    assert big_deals["blocked_domains"] == ["evil.example"]
    assert rivals["review_levels_always"] == ["low"]
    assert rivals["blocked_domains"] == ["evil.example", "competitor.example"]


def test_each_company_has_one_person_for_each_role() -> None:
    keys = [p.key for c in COMPANIES for p in c.people]
    ids = [p.user_id for c in COMPANIES for p in c.people] + [c.tenant_id for c in COMPANIES]
    assert len(set(keys)) == len(keys) == 9 and len(set(ids)) == len(ids) == 12
    for company in COMPANIES:
        roles = sorted(r for p in company.people for r in p.roles)
        assert roles == ["case_manager", "finance_approver", "legal_reviewer"]
        assert company.person("legal_reviewer").name == "Bob"


# --- tokens -------------------------------------------------------------------------------------


def test_tokens_are_written_for_every_person_and_sign_each_one_in(tmp_path: Path) -> None:
    tokens = write_tokens(tmp_path / "demo")
    api, sign_in = tmp_path / "demo" / "api-tokens.json", tmp_path / "demo" / "sign-in.json"
    for path in (api, sign_in):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(sign_in.read_text())["people"] == tokens
    assert all(t not in api.read_text() for t in tokens.values())  # hashes only
    auth = TokenAuthenticator.from_file(api)
    for company in COMPANIES:
        for person in company.people:
            who = auth.authenticate(tokens[person.key])
            assert (who.tenant_id, who.user_id) == (company.tenant_id, person.user_id)
            assert who.roles == frozenset(person.roles)


def test_tokens_are_new_each_time(tmp_path: Path) -> None:
    first, second = write_tokens(tmp_path / "a"), write_tokens(tmp_path / "b")
    assert set(first.values()).isdisjoint(second.values())


# --- the tamper fixture -------------------------------------------------------------------------


def test_the_fixture_verifies_broken_at_its_altered_event_and_only_there() -> None:
    fixture = fixtures.build(signer())
    stores = memory_stores()
    fixtures.install(stores, fixture)
    events = stores.audit.events(fixture.run_id)
    result = verify_chain(events)
    assert (result.ok, result.failed_seq) == (False, fixtures.TAMPERED_SEQ)
    assert [e.actor for e in events if e.actor == fixtures.TAMPERED_ACTOR] == ["mallory"]
    assert (
        fixture.run.status.value == "completed"
        and fixture.run.tenant_id == BY_KEY["careful"].tenant_id
    )
    assert fixtures.OBJECTIVE.endswith("(Pre-built tamper fixture.)")


def test_the_fixture_rates_a_contract_no_scene_uses() -> None:
    from gawe.demo.scenes import SCENES

    fixture = fixtures.build(signer())
    rated = {s.resolved_args.get("contract_id") for s in fixture.steps}
    scripts = [x for scene in SCENES for x in (scene.honest, scene.fooled) if x is not None]
    used = {
        step["args"]["contract_id"]
        for script in scripts
        for step in script.plan["steps"]
        if "contract_id" in step["args"]
    }
    assert rated == {"C-1004"} and "C-1004" not in used


def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str) -> dict[str, str]:
    tokens = write_tokens(tmp_path / "demo")
    for name, value in {
        "GAWE_APP": "gawe.apps.contract_review:bundle",
        "GAWE_POLICY_SIGNING_KEY": secrets.token_hex(32),
        "GAWE_API_TOKENS_FILE": str(tmp_path / "demo" / "api-tokens.json"),
        "GAWE_LLM_PROVIDER": provider,
        "GAWE_STORE": "memory",
        "GAWE_TENANT_SETTINGS": str(SETTINGS_FILE),
        "GAWE_DEMO_DIR": str(tmp_path / "data"),
        "GAWE_AUTO_RESUME": "0",
    }.items():
        monkeypatch.setenv(name, value)
    return tokens


def runs_seen_by_careful_alice(app: Any, tokens: dict[str, str]) -> list[dict[str, Any]]:
    http = TestClient(app)
    headers = {"Authorization": f"Bearer {tokens['careful-alice']}"}
    runs = http.get("/v1/runs", headers=headers).json()
    for run in runs:
        run["verify"] = http.get(f"/v1/runs/{run['run_id']}/audit/verify", headers=headers).json()
    return runs


def test_the_api_in_demo_mode_starts_with_the_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from gawe.api.main import create_app_from_env

    tokens = env(monkeypatch, tmp_path, "demo")
    app = create_app_from_env()
    (run,) = runs_seen_by_careful_alice(app, tokens)
    assert TestClient(app).get("/v1/demo").json()["fixture"]["run_id"] == run["run_id"]
    assert run["objective"] == fixtures.OBJECTIVE
    assert run["verify"]["ok"] is False and run["verify"]["failed_seq"] == fixtures.TAMPERED_SEQ


def test_without_the_demo_model_there_is_no_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from gawe.api import main
    from gawe.llm.fake import FakeProvider

    tokens = env(monkeypatch, tmp_path, "anthropic")
    monkeypatch.setattr(main, "_provider_from_env", lambda: FakeProvider([]))
    assert runs_seen_by_careful_alice(main.create_app_from_env(), tokens) == []
