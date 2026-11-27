"""M23: controls the agent can't touch.

GAWE's controls (policy rules, sources and variables, verifiers, revokers, guarantees, dispatch
conditions, the prepare-only setting, credentials, tenant settings, keys) are fixed when the
service is built. These tests pin every path that could change one: the API routes, the
maintenance commands, and the tools. And one path across apps: the IT app can grant access to the
system that holds contract review's deal values, so a change there waits for a person.
"""

import dataclasses
from pathlib import Path

import pytest

from gawe.api.wiring import AppBundle
from gawe.apps import contract_review, event_replay, it_access
from gawe.apps.contract_review import tools as cr_tools
from gawe.apps.event_replay import tools as er_tools
from gawe.apps.it_access import tools as it_tools

# Every route, and what it may change. None takes or changes a control; a new route must be
# added here, which is the review this test forces.
ROUTES = {
    ("GET", "/healthz"),
    ("GET", "/v1/me"),
    ("GET", "/v1/tools"),
    ("POST", "/v1/runs"),  # starts a run
    ("GET", "/v1/runs"),
    ("GET", "/v1/runs/{run_id}"),
    ("GET", "/v1/runs/{run_id}/verification"),
    ("GET", "/v1/runs/{run_id}/steps/{step_id}/trace"),
    ("GET", "/v1/runs/{run_id}/steps/{step_id}/undo"),  # reads captured state and dependents
    ("POST", "/v1/runs/{run_id}/steps/{step_id}/undo"),  # starts a policy-governed reversal
    ("POST", "/v1/runs/{run_id}/cancel"),  # ends a run
    ("POST", "/v1/runs/{run_id}/resume"),  # drives a run
    ("GET", "/v1/runs/{run_id}/audit"),
    ("GET", "/v1/runs/{run_id}/audit/verify"),
    ("GET", "/v1/approvals"),
    ("POST", "/v1/approvals/{approval_id}/decision"),  # decides one approval
    ("GET", "/v1/handoffs"),
    ("POST", "/v1/handoffs/{run_id}/{step_id}"),  # marks one hand-off
    ("GET", "/v1/review-items"),
    ("POST", "/v1/review-items/{item_id}/verdict"),  # records one verdict
    ("GET", "/v1/runs/{run_id}/events"),
}


def test_no_api_route_changes_a_control() -> None:
    from tests.api_bundle import bundle
    from tests.helpers import signer

    from gawe.api.app import create_app
    from gawe.api.auth import TokenAuthenticator
    from gawe.api.background import InlineBackground
    from gawe.api.wiring import assemble, memory_stores
    from gawe.llm.fake import FakeProvider

    b, _ = bundle()
    service = assemble(
        b,
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider([]),
        background=InlineBackground(),
    )
    app = create_app(service=service, authenticator=TokenAuthenticator({}))
    found = {
        (method, route.path)
        for route in app.routes
        if route.path.startswith(("/v1", "/healthz"))
        for method in getattr(route, "methods", ()) - {"HEAD"}
    }
    assert found == ROUTES


def test_the_maintenance_commands_are_sweep_and_end_old_grants() -> None:
    from gawe.maintenance import __main__ as cli

    with pytest.raises(SystemExit):
        cli.main(["--help"])
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert source.count("commands.add_parser(") == 2
    assert '"sweep"' in source and '"end-old-grants"' in source


def test_an_app_bundle_cant_be_changed_once_built(tmp_path: Path) -> None:
    b = it_access.bundle(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.prepare_only = {("iam.grant_access", "2.0.0"): "security_admin"}  # type: ignore[misc]
    assert dataclasses.fields(AppBundle)  # every control is a field of this frozen bundle


@pytest.mark.parametrize(
    "tools", [cr_tools, it_tools, er_tools], ids=["contract_review", "it_access", "event_replay"]
)
def test_no_tool_writes_a_gawe_control(tools: object) -> None:
    # Controls live in GAWE's configuration and records (`config.*`, `run.*`, `gawe.*`); tools
    # write only to target systems.
    for m in tools.MANIFESTS:  # type: ignore[attr-defined]
        for written in m.writes or ():
            assert not written.startswith(("config.", "run.", "gawe.")), (m.name, written)


# The IAM system's name for each system whose records another app's policy reads. Kept by hand:
# contract review reads deal values from the contract system, which the IAM calls `crm`; event
# replay reads the billing platform's daily total, and its preparation trusts the platform's
# events (M25), which the IAM calls `billing`.
HOLDS_POLICY_SOURCES = {"clm": "crm", "billing": "billing"}


def test_every_system_holding_a_policy_source_is_a_control_system(tmp_path: Path) -> None:
    sources = [
        *contract_review.bundle(tmp_path / "cr").policy_sources.values(),
        *event_replay.bundle(tmp_path / "er").policy_sources.values(),
    ]
    systems = {
        HOLDS_POLICY_SOURCES[s.resource.split(".")[0]]
        for s in sources
        if s.kind in ("resource", "counter")
    }
    assert systems == {"crm", "billing"}
    assert systems <= set(it_access.TENANT_VARIABLES["tenant"]["control_systems"])


@pytest.mark.parametrize(("system", "level"), [("wiki", "read"), ("crm", "write")])
def test_a_missing_control_systems_setting_fails_closed(
    tmp_path: Path, system: str, level: str
) -> None:
    from tests.helpers import bound_args, current_manifest, make_ctx, signer
    from tests.unit.test_apps_it_access import DANA, WIKI_READ

    from gawe.api.wiring import load_app_rules
    from gawe.core.policy.engine import RuleEngine

    b = it_access.bundle(tmp_path)
    engine = RuleEngine(load_app_rules(b), signer())
    manifest = current_manifest(b.manifests, "iam.grant_access")
    args = bound_args(manifest, {"email": DANA, "system": system, "level": level})
    tenant = {
        k: v for k, v in it_access.TENANT_VARIABLES["tenant"].items() if k != "control_systems"
    }
    request = {
        **it_access.request_variables(WIKI_READ),
        "submitters": [DANA],
        "portal_submitters": [DANA],
    }
    decision = engine.evaluate(
        make_ctx(
            manifest,
            args,
            roles=["it_agent"],
            variables={"tenant": tenant, "request": request},
        )
    )
    assert decision.outcome == "deny"  # not knowing which systems hold controls allows no grant


# --- end to end ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "level", "waits"),
    [
        ("crm", "write", True),
        ("crm", "admin", True),
        ("crm", "read", False),
        ("wiki", "write", False),
    ],
)
def test_a_grant_that_could_change_another_apps_controls_waits_for_security(
    tmp_path: Path, system: str, level: str, waits: bool
) -> None:
    from tests.unit.test_trusted_submitter import (
        AGENT,
        DANA,
        SECURITY,
        Desk,
        findings,
        grant_plan,
    )

    d = Desk(tmp_path, [findings(DANA), grant_plan(DANA, system, level)])
    view = d.run(DANA)  # Dana's own ticket, through the portal: otherwise automatic
    if not waits:
        assert view.status == "completed" and len(d.grants()) == 1
        return
    assert view.status == "paused_for_approval" and d.grants() == []
    assert "control-system-change-needs-security" in view.steps[-1].policy_rule_ids
    (item,) = d.service.inbox(SECURITY)
    d.service.decide(SECURITY, item.approval_id, decision="approved", action_hash=item.action_hash)
    assert d.service.get_run(AGENT, view.run_id).status == "completed" and len(d.grants()) == 1
