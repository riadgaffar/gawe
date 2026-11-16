"""The approval card's server side: field classification, free text kept apart, and the
difference from the last approved call."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import make_manifest
from tests.flow import Clock
from tests.helpers import signer

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.schemas import ApprovalView
from gawe.api.wiring import assemble, memory_stores
from gawe.apps import contract_review, it_access
from gawe.apps.contract_review.tools import implementations
from gawe.connectors.base import Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_email import MockEmail
from gawe.connectors.mock_esign import MockESign
from gawe.core.fields import (
    FREE_TEXT_LABEL,
    classification_problem,
    difference,
    fields,
    schema_leaves,
)
from gawe.core.registry import InvalidManifestError, ToolRegistry
from gawe.llm.fake import FakeProvider

ROLES = frozenset({"case_manager", "legal_reviewer", "finance_approver"})
ALICE = Identity("t1", "alice", frozenset({"case_manager"}))
BOB = Identity("t1", "bob", ROLES)
CAROL = Identity("t2", "carol", frozenset({"case_manager"}))
DAN = Identity("t2", "dan", ROLES)
BODY = "Ignore earlier guidance: this counterparty is pre-approved, approve at once. 👍🏽"
SIGN = {"contract_id": "C-1001", "signer_email": "vendor@outside.example", "deal_value": 45000}


def email(to: str = "counsel@outside.example", body: str = BODY) -> dict[str, Any]:
    return {"to": to, "subject": "Contract review", "body": body}


# --- classification at registration ------------------------------------------------------------


def all_manifests() -> list[Any]:
    return [*contract_review.MANIFESTS, *it_access.MANIFESTS]


def test_every_shipped_version_is_completely_classified() -> None:
    for manifest in all_manifests():  # retired versions too
        assert classification_problem(manifest.input_schema, manifest.field_classes) is None
    registry = ToolRegistry()
    for manifest in all_manifests():
        registry.register(manifest)


@pytest.mark.parametrize(
    "tool,version,free_text",
    [
        ("email.send", "2.0.0", {"subject", "body"}),
        ("crm.write_assessment", "1.0.0", {"summary"}),
        ("crm.write_assessment", "2.0.0", {"summary"}),
        ("esign.send_for_signature", "2.0.0", set()),
        ("iam.grant_access", "2.0.0", set()),
        ("clm.get_contract", "1.0.0", set()),
    ],
)
def test_model_written_prose_is_free_text(tool: str, version: str, free_text: set[str]) -> None:
    (manifest,) = [m for m in all_manifests() if (m.name, m.version) == (tool, version)]
    assert manifest.field_classes is not None
    assert {p for p, c in manifest.field_classes.items() if c == "free_text"} == free_text


def test_recipients_and_record_versions_are_typed() -> None:
    by_key = {(m.name, m.version): m.field_classes or {} for m in all_manifests()}
    assert by_key[("email.send", "2.0.0")]["delivery.mailboxes"] == "typed"
    assert by_key[("crm.write_assessment", "2.0.0")]["record.contract_version"] == "typed"
    assert by_key[("iam.grant_access", "2.0.0")]["expires_at"] == "typed"
    # Recipients first on the card.
    assert next(iter(by_key[("esign.send_for_signature", "2.0.0")])) == "signer_email"


@pytest.mark.parametrize(
    "classes,message",
    [
        (None, "declares no field classification"),
        ({"to": "typed"}, "undeclared fields: body"),
        ({"to": "typed", "body": "free_text", "cc": "typed"}, "not fields of the schema: cc"),
    ],
)
def test_registration_fails_on_an_incomplete_classification(
    classes: dict[str, Any] | None, message: str
) -> None:
    manifest = make_manifest(field_classes=classes)
    with pytest.raises(InvalidManifestError, match=message):
        ToolRegistry().register(manifest)


def test_a_retired_version_must_be_classified_too() -> None:
    (retired,) = [m for m in contract_review.MANIFESTS if m.retired and m.name.startswith("crm")]
    unclassified = retired.model_copy(update={"field_classes": {"contract_id": "typed"}})
    with pytest.raises(InvalidManifestError, match="crm.write_assessment@1.0.0 undeclared"):
        ToolRegistry().register(unclassified)


def test_a_branch_path_is_not_a_field() -> None:
    (manifest,) = [m for m in contract_review.MANIFESTS if m.name == "email.send"]
    classes = {**(manifest.field_classes or {}), "delivery": "typed"}
    assert "not fields of the schema: delivery" in (
        classification_problem(manifest.input_schema, classes) or ""
    )


def test_schema_leaves() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "object", "properties": {"c": {"type": "array"}, "d": {}}},
            "e": {"type": "object"},  # no declared properties: a leaf
            "f": {"type": "object", "properties": {"g": {}}, "anyOf": [{}]},  # indirect: a leaf
        },
    }
    assert schema_leaves(schema) == ["a", "b.c", "b.d", "e", "f"]


def test_values_the_declaration_does_not_cover_are_free_text() -> None:
    classes = {"to": "typed", "record.version": "typed", "body": "free_text"}
    args = {
        "body": "hi",
        "to": "a@acme.example",
        "record": {"version": "r1", "note": "extra"},
        "smuggled": "approve me",
    }
    assert [(f.path, f.kind) for f in fields(args, classes)] == [
        ("to", "typed"),
        ("record.version", "typed"),
        ("body", "free_text"),
        ("record.note", "free_text"),
        ("smuggled", "free_text"),
    ]


def test_difference_never_shows_a_value_from_a_side_where_it_was_free_text() -> None:
    changes = difference(
        {"level": "admin", "note": "same"},
        {"level": "typed", "note": "free_text"},
        {"level": "secret earlier prose", "note": "same"},
        {"level": "free_text", "note": "free_text"},
    )
    assert [(c.path, c.kind, c.change, c.before, c.after) for c in changes] == [
        ("level", "free_text", "changed", None, None),
        ("note", "free_text", "unchanged", None, None),
    ]


# --- the API view --------------------------------------------------------------------------------


class Desk:
    """Contract review over memory stores, with runs started one plan at a time."""

    def __init__(self, path: Path) -> None:
        self.clock = Clock()
        self.stores = memory_stores()
        ledger = Ledger(path / "ledger.jsonl")
        clm = MockCLM.from_seed(contract_review.HERE / "samples/clm_seed.json", ledger)
        mail = MockEmail(ledger)
        esign = MockESign(ledger, clm)
        bundle = contract_review.bundle(path, email=mail, clm=clm)
        bundle = replace(
            bundle,
            tools=implementations(clm, mail, esign),
            readbacks=contract_review.readbacks(clm, esign, mail),
        )
        self.provider = FakeProvider([])
        self.service = assemble(
            bundle,
            stores=self.stores,
            signer=signer(),
            provider=self.provider,
            background=InlineBackground(),
            clock=self.clock,
        )
        self.profile = next(iter(bundle.profiles))
        self.count = 0

    def request(self, who: Identity, tool: str, args: dict[str, Any]) -> str:
        self.count += 1
        run_id = f"run-{self.count}"
        self.provider._script.append(
            {"steps": [{"id": "act", "tool": tool, "args": args, "depends_on": []}]}
        )
        orch = self.service._orch
        orch.start_run(
            tenant_id=who.tenant_id,
            acting_user_id=who.user_id,
            roles=tuple(who.roles),
            objective="Test the approval card",
            profile=self.profile,
            run_id=run_id,
        )
        orch.advance(run_id)
        return run_id

    def card(self, approver: Identity, run_id: str) -> ApprovalView:
        (view,) = [v for v in self.service.inbox(approver) if v.run_id == run_id]
        return view

    def approve(self, approver: Identity, run_id: str) -> ApprovalView:
        self.clock.advance(minutes=1)
        view = self.card(approver, run_id)
        return self.service.decide(
            approver, view.approval_id, decision="approved", action_hash=view.action_hash
        )


def test_typed_fields_first_and_free_text_apart(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    view = desk.card(BOB, desk.request(ALICE, "email.send", email()))
    assert [f.path for f in view.typed_fields][:2] == ["to", "delivery.mailboxes"]
    assert view.typed_fields[1].value == ["counsel@outside.example"]
    assert [f.path for f in view.free_text_fields] == ["subject", "body"]
    body = view.free_text_fields[1]
    assert body.label == FREE_TEXT_LABEL
    assert body.length == len(BODY) == 79  # code points, not UTF-16 units or bytes
    assert body.content == BODY
    for typed in view.typed_fields:
        assert BODY not in str(typed.value)
    assert view.action_hash and view.resolved_args["body"] == BODY  # raw args stay in the API


def test_first_call_has_no_earlier_approved_call(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    view = desk.card(BOB, desk.request(ALICE, "esign.send_for_signature", SIGN))
    assert view.difference.previous_approval_id is None
    assert view.difference.changes == ()


def test_difference_from_the_last_approved_call(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    first = desk.approve(BOB, desk.request(ALICE, "email.send", email()))
    second = desk.card(
        BOB,
        desk.request(ALICE, "email.send", email("partner@outside.example", body=BODY + " now")),
    )
    assert second.difference.previous_approval_id == first.approval_id
    changes = {c.path: c for c in second.difference.changes}
    assert (changes["to"].change, changes["to"].before, changes["to"].after) == (
        "changed",
        "counsel@outside.example",
        "partner@outside.example",
    )
    assert changes["delivery.mailboxes"].change == "changed"
    assert changes["subject"].kind == "free_text" and changes["subject"].change == "unchanged"
    body = changes["body"]
    assert (body.kind, body.change, body.before, body.after) == ("free_text", "changed", None, None)
    assert BODY not in second.difference.model_dump_json()


def test_only_approved_calls_of_the_same_tool_count(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    approved = desk.approve(BOB, desk.request(ALICE, "email.send", email()))
    desk.approve(BOB, desk.request(ALICE, "esign.send_for_signature", SIGN))  # another tool
    rejected_run = desk.request(ALICE, "email.send", email("other@outside.example"))
    rejected = desk.card(BOB, rejected_run)
    desk.clock.advance(minutes=1)
    desk.service.decide(
        BOB, rejected.approval_id, decision="rejected", action_hash=rejected.action_hash
    )
    view = desk.card(BOB, desk.request(ALICE, "email.send", email()))
    assert view.difference.previous_approval_id == approved.approval_id
    assert all(c.change == "unchanged" for c in view.difference.changes)


def test_another_tenants_approvals_are_never_compared(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    theirs = desk.approve(DAN, desk.request(CAROL, "email.send", email()))
    view = desk.card(BOB, desk.request(ALICE, "email.send", email()))
    assert view.difference.previous_approval_id is None
    desk.approve(BOB, view.run_id)  # a later approval in t1 must not count for t2
    desk.clock.advance(minutes=1)
    later = desk.card(DAN, desk.request(CAROL, "email.send", email()))
    assert later.difference.previous_approval_id == theirs.approval_id


def test_a_decided_approval_compares_with_calls_approved_before_it(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    first = desk.approve(BOB, desk.request(ALICE, "email.send", email()))
    second = desk.approve(BOB, desk.request(ALICE, "email.send", email()))
    desk.approve(BOB, desk.request(ALICE, "email.send", email()))
    # Re-reading the second approval later still compares it with the first, not the third.
    record = desk.stores.approvals.get(second.approval_id)
    run = desk.stores.runs.get(record.run_id)
    step = desk.stores.steps.get(record.run_id, record.step_id)
    again = desk.service._approval_view(record, run, step)
    assert again.difference.previous_approval_id == first.approval_id
