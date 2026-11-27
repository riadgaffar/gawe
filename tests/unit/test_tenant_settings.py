"""M29 S1: settings per tenant, from a file read once at startup.

Every app declares a schema and defaults equal to the values it always used, so a deployment
without a file decides as before. A file gives some tenants other values; a bad file stops startup.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.helpers import bound_args, current_manifest, make_ctx, prepared_email, signer
from tests.unit.test_record_versions import GATED
from tests.unit.test_write_readback import Env

from gawe.api.wiring import load_app_rules
from gawe.apps import contract_review as cr
from gawe.apps import event_replay as er
from gawe.apps import it_access as ia
from gawe.core.history import HistoryEntry
from gawe.core.policy.engine import RuleEngine
from gawe.core.runs import RunRecord
from gawe.core.statemachine import StepStatus
from gawe.core.tenant_settings import (
    SETTINGS_ENV,
    TenantSettings,
    TenantSettingsError,
    from_env,
    read_settings_file,
)

SCHEMA = {
    "type": "object",
    "properties": {"limit": {"type": "integer", "minimum": 1}, "names": {"type": "array"}},
    "required": ["limit", "names"],
    "additionalProperties": False,
}
DEFAULTS = {"limit": 2, "names": ["a"]}

CAREFUL, BIG_DEALS, RIVALS = "t-careful", "t-big-deals", "t-rivals"
COMPANIES = {
    CAREFUL: {"blocked_domains": ["evil.example"]},
    BIG_DEALS: {
        "blocked_domains": ["evil.example"],
        "review_levels_always": [],
        "large_deal_threshold": 250_000,
    },
    RIVALS: {"blocked_domains": ["evil.example", "competitor.example"]},
}


def run_for(tenant: str, findings: dict[str, Any] | None = None) -> RunRecord:
    return RunRecord(
        run_id="r",
        tenant_id=tenant,
        acting_user_id="u",
        objective="o",
        profile="contract_review",
        created_at=datetime(2026, 11, 26, tzinfo=UTC),
        findings=findings,
    )


# --- the core -----------------------------------------------------------------------------------


def test_a_tenant_not_named_gets_the_defaults_and_a_named_one_its_values() -> None:
    settings = TenantSettings(SCHEMA, DEFAULTS, {"t1": {"limit": 5}})
    assert settings.for_tenant("t1") == {"limit": 5, "names": ["a"]}
    assert settings.for_tenant("other") == DEFAULTS
    assert settings.for_tenant(None) == DEFAULTS
    assert settings.tenants == frozenset({"t1"})


def test_what_a_caller_gets_is_a_copy_it_cant_change_for_others() -> None:
    settings = TenantSettings(SCHEMA, DEFAULTS, {"t1": {"names": ["x"]}})
    got = settings.for_tenant("t1")
    got["names"].append("y")
    got["limit"] = 99
    assert settings.for_tenant("t1") == {"limit": 2, "names": ["x"]}
    assert settings.defaults() == DEFAULTS


@pytest.mark.parametrize(
    ("tenants", "message"),
    [
        ({"t1": {"limt": 5}}, "Additional properties"),
        ({"t1": {"limit": "5"}}, "is not of type 'integer'"),
        ({"t1": {"limit": 0}}, "less than the minimum"),
        ({"t1": ["limit"]}, "must be an object"),
        ({"": {"limit": 3}}, "non-empty string"),
    ],
    ids=["unknown key", "wrong type", "forbidden value", "not an object", "empty tenant id"],
)
def test_bad_tenant_settings_stop_startup(tenants: Any, message: str) -> None:
    with pytest.raises(TenantSettingsError, match=message):
        TenantSettings(SCHEMA, DEFAULTS, tenants)


def test_defaults_are_checked_and_a_schema_must_refuse_extra_keys() -> None:
    with pytest.raises(TenantSettingsError, match="the defaults"):
        TenantSettings(SCHEMA, {"limit": 2})
    open_schema = {**SCHEMA, "additionalProperties": True}
    with pytest.raises(TenantSettingsError, match="allows no extra keys"):
        TenantSettings(open_schema, DEFAULTS)


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"tenant": {}}', '{"tenants": [], "x": 1}', '{"tenants": []}'],
)
def test_a_settings_file_of_the_wrong_shape_stops_startup(tmp_path: Path, content: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(TenantSettingsError):
        read_settings_file(path)


def test_from_env_reads_the_file_named_and_otherwise_the_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SETTINGS_ENV, raising=False)
    assert from_env(SCHEMA, DEFAULTS).tenants == frozenset()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"tenants": {"t1": {"limit": 7}}}), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_ENV, str(path))
    assert from_env(SCHEMA, DEFAULTS).for_tenant("t1")["limit"] == 7
    monkeypatch.setenv(SETTINGS_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(TenantSettingsError):
        from_env(SCHEMA, DEFAULTS)


# --- each app: defaults are today's values, a file changes outcomes per tenant ------------------


@pytest.mark.parametrize("app", [cr, ia, er], ids=lambda m: m.PROFILE_NAME)
def test_defaults_are_the_values_each_app_always_used(app: Any, tmp_path: Path) -> None:
    b = app.bundle(tmp_path)
    assert b.tenant_settings.defaults() == dict(app.TENANT_VARIABLES["tenant"])
    assert b.tenant_settings.tenants == frozenset()


@pytest.mark.parametrize("app", [cr, ia, er], ids=lambda m: m.PROFILE_NAME)
def test_a_bad_settings_file_stops_each_app_from_starting(
    app: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"tenants": {"t1": {"no_such_setting": 1}}}), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_ENV, str(path))
    with pytest.raises(TenantSettingsError, match="no_such_setting"):
        app.bundle(tmp_path)


def companies() -> TenantSettings:
    return TenantSettings(cr.SETTINGS_SCHEMA, cr.DEFAULT_SETTINGS, COMPANIES)


def decide(
    tenant: str, tool: str, args: dict[str, Any], tmp_path: Path, findings: Any = None
) -> Any:
    b = cr.bundle(tmp_path, settings=companies())
    engine = RuleEngine(load_app_rules(b), signer())
    manifest = current_manifest(b.manifests, tool)
    run = run_for(tenant, findings)
    variables = {**b.variables(run), "history": {"lower_ratings": []}}
    return engine.evaluate(
        make_ctx(manifest, bound_args(manifest, args), roles=["case_manager"], variables=variables)
    )


def assessment(contract: str, level: str) -> dict[str, Any]:
    return {"contract_id": contract, "risk_level": level, "summary": "s"}


@pytest.mark.parametrize(
    ("tenant", "outcome"),
    [(CAREFUL, "needs_approval"), (BIG_DEALS, "allow"), (RIVALS, "needs_approval")],
)
def test_a_low_rating_on_a_small_deal_needs_legal_only_where_the_company_says(
    tenant: str, outcome: str, tmp_path: Path
) -> None:
    decision = decide(tenant, "crm.write_assessment", assessment("C-1001", "low"), tmp_path)
    assert decision.outcome == outcome
    assert ("low-risk-needs-legal" in decision.rule_ids) == (tenant != BIG_DEALS)


@pytest.mark.parametrize(
    ("tenant", "large"),
    [(CAREFUL, True), (BIG_DEALS, False)],  # 250,000: over Careful's line, not over Big-Deals'
)
def test_the_large_deal_line_is_the_companys(tenant: str, large: bool, tmp_path: Path) -> None:
    decision = decide(tenant, "crm.write_assessment", assessment("C-1002", "low"), tmp_path)
    assert ("low-risk-large-deal-needs-legal" in decision.rule_ids) is large


@pytest.mark.parametrize(
    ("tenant", "outcome"),
    [(CAREFUL, "needs_approval"), (BIG_DEALS, "needs_approval"), (RIVALS, "deny")],
)
def test_a_competitors_domain_is_blocked_only_where_the_company_blocks_it(
    tenant: str, outcome: str, tmp_path: Path
) -> None:
    email = prepared_email({"to": "someone@competitor.example", "subject": "s", "body": "b"})
    decision = decide(tenant, "email.send", email, tmp_path)
    assert decision.outcome == outcome
    assert ("blocked-domain-email-denied" in decision.rule_ids) == (tenant == RIVALS)


def test_a_tenant_not_in_the_file_decides_as_today(tmp_path: Path) -> None:
    decision = decide("t-unlisted", "crm.write_assessment", assessment("C-1001", "low"), tmp_path)
    assert "low-risk-needs-legal" in decision.rule_ids
    email = prepared_email({"to": "someone@competitor.example", "subject": "s", "body": "b"})
    assert decide("t-unlisted", "email.send", email, tmp_path).outcome == "deny"


def test_captured_variables_use_the_judged_runs_company(tmp_path: Path) -> None:
    """M27's captured deal value is judged against the run's company's line, not a shared one."""
    e = Env(tmp_path, args=GATED["crm"])  # C-1002, 250,000
    e.advance()
    args = e.stores.steps.get(e.run_id, "write").resolved_args
    captured = cr.captured_variables_for(companies())
    careful = captured(run_for(CAREFUL), args)
    big_deals = captured(run_for(BIG_DEALS), args)
    assert careful["review"]["large_deals"] == ["C-1002"]  # over 100,000
    assert big_deals["review"]["large_deals"] == []  # not over 250,000
    assert big_deals["tenant"]["review_levels_always"] == []


def test_a_missing_setting_denies(tmp_path: Path) -> None:
    b = cr.bundle(tmp_path)
    engine = RuleEngine(load_app_rules(b), signer())
    manifest = current_manifest(b.manifests, "crm.write_assessment")
    tenant = {k: v for k, v in cr.DEFAULT_SETTINGS.items() if k != "review_levels_always"}
    variables = {
        "tenant": tenant,
        "review": {"unsupported_levels": [], "large_deals": []},
        "history": {"lower_ratings": []},
    }
    args = bound_args(manifest, assessment("C-1001", "medium"))
    decision = engine.evaluate(
        make_ctx(manifest, args, roles=["case_manager"], variables=variables)
    )
    assert decision.outcome == "deny" and decision.rule_ids == ("engine.error",)


def test_it_access_counts_grants_against_the_runs_companys_settings(tmp_path: Path) -> None:
    settings = TenantSettings(
        ia.SETTINGS_SCHEMA, ia.DEFAULT_SETTINGS, {"strict": {"automatic_grants": 1}}
    )
    history = ia.grant_history_for(settings)
    entry = HistoryEntry(
        run_id="r0",
        step_id="grant",
        tool_version="2.0.0",
        action_hash="a" * 64,
        status=StepStatus.SUCCEEDED,
        resolved_args={"email": "a@acme.example", "system": "wiki", "level": "read"},
        started_at=datetime(2026, 11, 25, tzinfo=UTC),
    )
    assert history([entry], {}, run_for("strict")) == {"repeated_systems": ["wiki"]}
    assert history([entry], {}, run_for("default")) == {"repeated_systems": []}


def test_event_replay_reads_the_runs_companys_limits(tmp_path: Path) -> None:
    settings = TenantSettings(
        er.SETTINGS_SCHEMA, er.DEFAULT_SETTINGS, {"small": {"automatic_total": 100}}
    )
    b = er.bundle(tmp_path, settings=settings)
    assert b.variables(run_for("small"))["tenant"]["automatic_total"] == 100
    assert b.variables(run_for("default"))["tenant"]["automatic_total"] == 1_000
