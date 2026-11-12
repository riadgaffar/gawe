from pathlib import Path
from typing import Any

import pytest
from tests.helpers import make_ctx, signer

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, memory_stores
from gawe.apps import it_access as ia
from gawe.connectors.base import Ledger
from gawe.core.policy.engine import RuleEngine
from gawe.core.policy.rules import load_rules
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


def start(svc: Any, who: Identity = AGENT) -> str:
    return svc.start_run(
        who, objective="Process the request", profile="it_access", document="ticket text"
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


def test_external_account_is_denied(tmp_path: Path) -> None:
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
    assert svc.get_run(AGENT, start(svc)).status == "denied" and grants(tmp_path) == []


def test_missing_role_is_denied_before_policy_rules(tmp_path: Path) -> None:
    svc = service(tmp_path, [WIKI_READ, plan("dana.ortiz@acme.example", "wiki", "read")])
    view = svc.get_run(NO_ROLE, start(svc, NO_ROLE))
    assert view.status == "denied" and grants(tmp_path) == []
    assert view.steps[-1].policy_rule_ids == ("rbac.missing_role",)


@pytest.mark.parametrize(
    ("system", "level", "outcome"),
    [
        ("wiki", "read", "allow"),
        ("crm", "write", "allow"),
        ("billing", "admin", "needs_approval"),
        ("prod_db", "read", "needs_approval"),
    ],
)
def test_policy_golden_cases(system: str, level: str, outcome: str) -> None:
    b = ia.bundle(Path("/nonexistent-never-written"))
    engine = RuleEngine(load_rules(b.policy_rules, manifests=b.manifests), signer())
    manifest = {m.name: m for m in b.manifests}["iam.grant_access"]
    args = {"email": "dana.ortiz@acme.example", "system": system, "level": level}
    decision = engine.evaluate(
        make_ctx(manifest, args, roles=["it_agent"], variables=dict(ia.TENANT_VARIABLES))
    )
    assert decision.outcome == outcome
