from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.helpers import bound_args, current_manifest, make_ctx, signer

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, load_app_rules, memory_stores
from gawe.apps import it_access as ia
from gawe.connectors.base import Ledger
from gawe.core.policy.engine import RuleEngine
from gawe.llm.fake import FakeProvider

T = "t1"
AGENT = Identity(T, "ivy", frozenset({"it_agent"}))
NO_ROLE = Identity(T, "nora", frozenset({"case_manager"}))
SEC = Identity(T, "sec", frozenset({"security_admin"}))

WIKI_READ = {
    "requester_email": "dana.ortiz@acme.example",
    "system": "wiki",
    "level": "read",
    "justification": "Needs the month-end close runbook.",
}
PROD_ADMIN = {
    **WIKI_READ,
    "requester_email": "sam.okafor@acme.example",
    "system": "prod_db",
    "level": "admin",
}


DANA = "dana.ortiz@acme.example"


def plan(email: str, system: str, level: str) -> dict[str, Any]:
    return {
        "steps": [
            {"id": "who", "tool": "iam.lookup_user", "args": {"email": email}, "depends_on": []},
            {
                "id": "grant",
                "tool": "iam.grant_access",
                "args": {"email": email, "system": system, "level": level},
                "depends_on": ["who"],
            },
        ]
    }


def service(tmp_path: Path, script: list[Any]) -> Any:
    return assemble(
        ia.bundle(tmp_path),
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider(script),
        background=InlineBackground(),
    )


def grants(tmp_path: Path) -> list[dict[str, Any]]:
    return Ledger(tmp_path / "ledger.jsonl").records("iam.grant")


def ticket(svc: Any, who: Identity = AGENT, submitted_by: str = DANA, **kw: Any) -> str:
    """File a ticket in the app's mock ticket system; returns its id."""
    ticket_id = f"T-{uuid4().hex[:8]}"
    svc._sources["it_access"].create(
        who.tenant_id,
        ticket_id,
        submitted_by=submitted_by,
        channel=kw.pop("channel", "portal"),
        body=kw.pop("body", "ticket text"),
        **kw,
    )
    return ticket_id


def start(svc: Any, who: Identity = AGENT, **ticket_fields: Any) -> str:
    source_id = ticket(svc, who, **ticket_fields)
    return svc.start_run(
        who, objective="Process the request", profile="it_access", source_id=source_id
    ).run_id


def test_wiki_read_is_granted_without_approval(tmp_path: Path) -> None:
    svc = service(tmp_path, [WIKI_READ, plan("dana.ortiz@acme.example", "wiki", "read")])
    run_id = start(svc)
    assert svc.get_run(AGENT, run_id).status == "completed"
    assert len(grants(tmp_path)) == 1


def test_prod_admin_waits_for_security(tmp_path: Path) -> None:
    svc = service(tmp_path, [PROD_ADMIN, plan("sam.okafor@acme.example", "prod_db", "admin")])
    run_id = start(svc)
    assert svc.get_run(AGENT, run_id).status == "paused_for_approval" and grants(tmp_path) == []
    (item,) = svc.inbox(SEC)
    assert item.approver_role == "security_admin"
    svc.decide(SEC, item.approval_id, decision="approved", action_hash=item.action_hash)
    assert svc.get_run(AGENT, run_id).status == "completed" and len(grants(tmp_path)) == 1


def test_unknown_external_account_fails_in_preparation(tmp_path: Path) -> None:
    svc = service(
        tmp_path,
        [
            WIKI_READ,
            {
                "steps": [
                    {
                        "id": "grant",
                        "tool": "iam.grant_access",
                        "args": {
                            "email": "someone@gmail.example",
                            "system": "wiki",
                            "level": "read",
                        },
                        "depends_on": [],
                    }
                ]
            },
        ],
    )
    # The account is not in the IAM system, so trusted preparation cannot bind its revision and
    # the step fails before policy. Policy's own external-account rule is tested below.
    view = svc.get_run(AGENT, start(svc))
    assert view.status == "failed" and grants(tmp_path) == []
    assert view.steps[-1].error == "user_not_found"


def test_policy_still_denies_an_external_account() -> None:
    b = ia.bundle(Path("/nonexistent-never-written"))
    manifest = current_manifest(b.manifests, "iam.grant_access")
    decision = RuleEngine(load_app_rules(b), signer()).evaluate(
        make_ctx(
            manifest,
            bound_args(manifest, {"email": "a@outside.example", "system": "wiki", "level": "read"}),
            roles=["it_agent"],
            variables={
                **ia.TENANT_VARIABLES,
                "request": {
                    "reported_requester": "a@outside.example",
                    "submitters": ["a@outside.example"],
                    "portal_submitters": ["a@outside.example"],
                },
            },
        )
    )
    assert decision.outcome == "deny" and "external-user-denied" in decision.rule_ids


def test_missing_role_is_denied_before_policy_rules(tmp_path: Path) -> None:
    svc = service(tmp_path, [WIKI_READ, plan("dana.ortiz@acme.example", "wiki", "read")])
    view = svc.get_run(NO_ROLE, start(svc, NO_ROLE))
    assert view.status == "denied" and grants(tmp_path) == []
    assert view.steps[-1].policy_rule_ids == ("rbac.missing_role",)


@pytest.mark.parametrize(
    ("system", "level", "outcome"),
    [
        ("wiki", "read", "allow"),
        ("wiki", "write", "allow"),
        ("crm", "read", "allow"),
        # M23: the CRM holds contract review's deal values, a control of that app.
        ("crm", "write", "needs_approval"),
        ("crm", "admin", "needs_approval"),
        ("billing", "admin", "needs_approval"),
        ("prod_db", "read", "needs_approval"),
    ],
)
def test_policy_golden_cases(system: str, level: str, outcome: str) -> None:
    b = ia.bundle(Path("/nonexistent-never-written"))
    engine = RuleEngine(load_app_rules(b), signer())
    manifest = current_manifest(b.manifests, "iam.grant_access")
    args = bound_args(
        manifest, {"email": "dana.ortiz@acme.example", "system": system, "level": level}
    )
    decision = engine.evaluate(
        make_ctx(
            manifest,
            args,
            roles=["it_agent"],
            variables={
                **ia.TENANT_VARIABLES,
                "request": {
                    **ia.request_variables(WIKI_READ),
                    "submitters": [DANA],
                    "portal_submitters": [DANA],
                },
            },
        )
    )
    assert decision.outcome == outcome


@pytest.mark.parametrize(
    "reported",
    [
        None,
        {},
        {"requester_email": None},
        {"requester_email": ""},
        {"requester_email": True},
        {"requester_email": []},
        {"requester_email": "not-an-address"},
        {"requester_email": "a@acme.example\n"},
    ],
)
def test_unusable_reported_requester_cannot_authorize_a_grant(reported: Any) -> None:
    assert ia.request_variables(reported) == {}
    b = ia.bundle(Path("/nonexistent-never-written"))
    engine = RuleEngine(load_app_rules(b), signer())
    decision = engine.evaluate(
        make_ctx(
            current_manifest(b.manifests, "iam.grant_access"),
            bound_args(
                current_manifest(b.manifests, "iam.grant_access"),
                {"email": "dana.ortiz@acme.example", "system": "wiki", "level": "read"},
            ),
            roles=["it_agent"],
            variables={**ia.TENANT_VARIABLES, "request": ia.request_variables(reported)},
        )
    )
    assert decision.outcome == "deny" and decision.rule_ids == ("engine.error",)


@pytest.mark.parametrize(
    "reported,proposed,internal,outcome",
    [
        ("dana@acme.example", "dana@acme.example", "acme.example", "allow"),
        ("dana@acme.example", "sam@acme.example", "acme.example", "deny"),
        ("Dana@acme.example", "dana@acme.example", "acme.example", "deny"),
        ("dana@acme.example", "dana@ACME.EXAMPLE", "acme.example", "deny"),
        ("dana+tag@acme.example", "dana@acme.example", "acme.example", "deny"),
        ("DANA+tag@ACME.EXAMPLE", "DANA+tag@ACME.EXAMPLE", "acme.example", "allow"),
        ("élève@bücher.example", "élève@bücher.example", "xn--bcher-kva.example", "allow"),
        ("a@bücher.example", "a@xn--bcher-kva.example", "bücher.example", "deny"),
        ("a@outside.example", "a@outside.example", "acme.example", "deny"),
    ],
)
def test_grant_matches_reported_requester_exactly(
    reported: str, proposed: str, internal: str, outcome: str
) -> None:
    b = ia.bundle(Path("/nonexistent-never-written"))
    request = ia.request_variables({"requester_email": reported})
    assert request == {"reported_requester": reported}  # do not normalize the identity
    # The reported requester also filed the ticket, so only the Reader cross-check varies.
    request = {**request, "submitters": [reported], "portal_submitters": [reported]}
    decision = RuleEngine(load_app_rules(b), signer()).evaluate(
        make_ctx(
            current_manifest(b.manifests, "iam.grant_access"),
            bound_args(
                current_manifest(b.manifests, "iam.grant_access"),
                {"email": proposed, "system": "wiki", "level": "read"},
            ),
            roles=["it_agent"],
            variables={
                "tenant": {
                    "internal_domains": [internal],
                    "max_grant_lifetime_hours": 720,
                    "control_systems": ["crm"],
                },
                "request": request,
            },
        )
    )
    assert decision.outcome == outcome
    assert ("requester-mismatch-denied" in decision.rule_ids) == (reported != proposed)
    assert signer().verify(decision)


def test_fooled_planner_cannot_grant_to_a_different_person(tmp_path: Path) -> None:
    svc = service(tmp_path, [WIKI_READ, plan("sam.okafor@acme.example", "billing", "write")])
    view = svc.get_run(AGENT, start(svc))
    assert view.status == "denied" and grants(tmp_path) == []
    assert "requester-mismatch-denied" in view.steps[-1].policy_rule_ids


def test_a_fooled_readers_lateral_grant_waits_for_security(tmp_path: Path) -> None:
    """M16c: the Reader names Sam, but Dana filed the ticket, so the grant waits for a person.

    Before M16c this grant ran: the only check was against the Reader's own report."""
    findings = {**WIKI_READ, "requester_email": "sam.okafor@acme.example"}
    svc = service(tmp_path, [findings, plan("sam.okafor@acme.example", "billing", "write")])
    view = svc.get_run(AGENT, start(svc, submitted_by=DANA))
    assert view.status == "paused_for_approval" and grants(tmp_path) == []
    assert "grant-to-other-than-submitter-needs-security" in view.steps[-1].policy_rule_ids
    assert view.steps[-1].approver_role == "security_admin"


def test_requester_variables_are_bound_to_each_persisted_run(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from gawe.core.runs import RunRecord

    bundle = ia.bundle(tmp_path)
    run = RunRecord(
        run_id="one",
        tenant_id=T,
        acting_user_id="ivy",
        objective="Request",
        profile="it_access",
        created_at=datetime.now(UTC),
        findings=WIKI_READ,
    )
    other = run.model_copy(
        update={
            "run_id": "two",
            "findings": {**WIKI_READ, "requester_email": "sam.okafor@acme.example"},
        }
    )
    assert bundle.variables(run)["request"] == {"reported_requester": "dana.ortiz@acme.example"}
    assert bundle.variables(other)["request"] == {"reported_requester": "sam.okafor@acme.example"}
    assert bundle.variables(run.model_copy(update={"findings": None}))["request"] == {}
