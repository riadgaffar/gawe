"""M20 S3: listing and marking hand-offs, through the service and over HTTP. The rules are the
orchestrator's (S2); this layer adds the tenant boundary and maps refusals to status codes."""

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from gawe.api.app import create_app  # noqa: E402
from gawe.api.auth import Identity, TokenAuthenticator, hash_token, new_token  # noqa: E402
from gawe.api.errors import ConflictError, ForbiddenError, NotFoundError  # noqa: E402
from gawe.core.statemachine import RunStatus, StepStatus  # noqa: E402
from tests.unit.test_prepare_only import CRM_WRITE, prepare_only, write_step  # noqa: E402
from tests.unit.test_write_readback import ALICE, BOB, Env  # noqa: E402

CAROL = Identity("t1", "carol", frozenset({"case_manager"}))
ERIN = Identity("t2", "erin", frozenset({"legal_reviewer"}))  # same role, another tenant
ALICE_REVIEWER = Identity("t1", ALICE.user_id, frozenset({"legal_reviewer"}))  # the requester
PEOPLE = {"alice": ALICE, "alice2": ALICE_REVIEWER, "bob": BOB, "carol": CAROL, "erin": ERIN}


class Handoffs:
    def __init__(self, tmp_path: Path) -> None:
        self.e = Env(tmp_path, "crm")
        prepare_only(self.e, CRM_WRITE)
        self.e.advance()
        self.tokens = {name: new_token() for name in PEOPLE}
        auth = TokenAuthenticator({hash_token(self.tokens[n]): i for n, i in PEOPLE.items()})
        self.http = TestClient(create_app(service=self.e.service, authenticator=auth))

    def get(self, who: str) -> Any:
        return self.http.get("/v1/handoffs", headers=self.h(who))

    def mark(self, who: str, run_id: str | None = None, **body: Any) -> Any:
        body.setdefault("outcome", "done")
        body.setdefault("action_hash", write_step(self.e).action_hash)
        url = f"/v1/handoffs/{run_id or self.e.run_id}/write"
        return self.http.post(url, json=body, headers=self.h(who))

    def h(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


def test_the_role_holder_sees_it_typed_first_and_nobody_else_does(tmp_path: Path) -> None:
    c = Handoffs(tmp_path)
    [item] = c.get("bob").json()
    step = write_step(c.e)
    assert (item["run_id"], item["step_id"], item["tool"]) == (c.e.run_id, "write", CRM_WRITE[0])
    assert (item["role"], item["status"], item["requested_by"]) == (
        "legal_reviewer",
        "prepared",
        ALICE.user_id,
    )
    assert item["action_hash"] == step.action_hash and item["marked_by"] is None
    fields = item["typed_fields"] + item["free_text_fields"]
    shown = {f["path"] for f in fields}
    assert shown, "the prepared action is shown"
    for free in item["free_text_fields"]:
        assert free["label"] and free["length"] >= 0
    assert c.get("alice2").json() == []  # the requester, even holding the role
    assert c.get("carol").json() == []  # no role
    assert c.get("erin").json() == []  # another tenant


def test_marking_done_lets_the_run_finish_and_the_run_view_shows_who(tmp_path: Path) -> None:
    c = Handoffs(tmp_path)
    r = c.mark("bob", reference="ENV-2041")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["marked_by"], body["reference"]) == (
        "done_by_person",
        BOB.user_id,
        "ENV-2041",
    )
    assert c.e.stores.runs.get(c.e.run_id).status is RunStatus.COMPLETED  # kicked inline
    assert c.e.calls == [] and c.e.target.records() == []
    run = c.http.get(f"/v1/runs/{c.e.run_id}", headers=c.h("alice")).json()
    [view] = [s for s in run["steps"] if s["step_id"] == "write"]
    assert view["status"] == "done_by_person"
    assert view["handoff"]["role"] == "legal_reviewer"
    assert (view["handoff"]["marked_by"], view["handoff"]["reference"]) == (BOB.user_id, "ENV-2041")
    assert c.get("bob").json() == []


def test_marking_not_done_ends_the_run_denied(tmp_path: Path) -> None:
    c = Handoffs(tmp_path)
    assert c.mark("bob", outcome="not_done").json()["status"] == "not_done_by_person"
    run = c.e.stores.runs.get(c.e.run_id)
    assert run.status is RunStatus.DENIED and run.error == "handoff_not_done"


@pytest.mark.parametrize(
    ("who", "body", "status", "code"),
    [
        ("carol", {}, 403, "role_required"),
        ("alice2", {}, 403, "requester_cannot_mark"),
        ("bob", {"action_hash": "0" * 64}, 409, "action_hash_mismatch"),
        ("bob", {"reference": "has spaces"}, 422, "reference_invalid"),
        ("erin", {}, 404, None),  # another tenant's run does not exist for them
    ],
)
def test_refusals_map_to_status_codes_and_leave_it_prepared(
    tmp_path: Path, who: str, body: dict[str, Any], status: int, code: str | None
) -> None:
    c = Handoffs(tmp_path)
    r = c.mark(who, **body)
    assert r.status_code == status, r.text
    if code:
        assert r.json()["error"]["message"] == code
    assert write_step(c.e).status is StepStatus.PREPARED
    assert c.e.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"outcome": "approved"},  # not a dispatch, under any name
        {"outcome": "done", "action_hash": "short"},
        {"outcome": "done", "reference": "x" * 121},
        {"outcome": "done", "execute": True},
    ],
)
def test_the_body_is_strict(tmp_path: Path, body: dict[str, Any]) -> None:
    c = Handoffs(tmp_path)
    body.setdefault("action_hash", write_step(c.e).action_hash)
    r = c.http.post(f"/v1/handoffs/{c.e.run_id}/write", json=body, headers=c.h("bob"))
    assert r.status_code == 422
    assert write_step(c.e).status is StepStatus.PREPARED


def test_a_second_mark_conflicts(tmp_path: Path) -> None:
    c = Handoffs(tmp_path)
    assert c.mark("bob").status_code == 200
    r = c.mark("bob", outcome="not_done")
    assert r.status_code == 409 and r.json()["error"]["message"] == "not_waiting_for_a_person"


def test_steps_that_were_never_handed_off_are_not_found(tmp_path: Path) -> None:
    c = Handoffs(tmp_path)
    service = c.e.service
    with pytest.raises(NotFoundError):
        service.mark_handoff(BOB, c.e.run_id, "read", done=True, action_hash="0" * 64)
    with pytest.raises(NotFoundError):
        service.mark_handoff(BOB, "not-a-run", "write", done=True, action_hash="0" * 64)
    with pytest.raises(NotFoundError):
        service.mark_handoff(ERIN, c.e.run_id, "write", done=True, action_hash="0" * 64)
    step = write_step(c.e)
    with pytest.raises(ForbiddenError):
        service.mark_handoff(ALICE, c.e.run_id, "write", done=True, action_hash=step.action_hash)
    service.mark_handoff(BOB, c.e.run_id, "write", done=True, action_hash=step.action_hash)
    with pytest.raises(ConflictError):
        service.mark_handoff(BOB, c.e.run_id, "write", done=True, action_hash=step.action_hash)


def test_a_run_without_prepare_only_shows_no_hand_off(tmp_path: Path) -> None:
    e = Env(tmp_path, "crm")
    e.advance()
    assert e.service.handoffs(BOB) == []
    run = e.service.get_run(ALICE, e.run_id)
    assert all(s.handoff is None for s in run.steps)


def test_the_demo_a_high_value_signature_goes_to_a_person_and_asks_finance_nothing(
    tmp_path: Path,
) -> None:
    # docs/demo-script.md 5d: C-1002 for signature, with signatures prepare-only.
    from tests.unit.test_prepare_only import SIGNATURE

    sign = {"contract_id": "C-1002", "signer_email": "legal@globex.example", "deal_value": 250000}
    e = Env(tmp_path, "esign", args=sign)
    prepare_only(e, SIGNATURE)
    e.advance()
    step = write_step(e)
    assert step.status is StepStatus.PREPARED
    assert step.policy_decision is not None
    assert step.policy_decision.outcome == "needs_approval"
    assert step.policy_decision.approver_role == "finance_approver"
    fiona = Identity("t1", "fiona", frozenset({"finance_approver"}))
    assert e.service.inbox(fiona) == [] and e.service.handoffs(fiona) == []
    [item] = e.service.handoffs(BOB)
    e.service.mark_handoff(
        BOB, e.run_id, "write", done=True, action_hash=item.action_hash, reference="ENV-2041"
    )
    assert e.stores.runs.get(e.run_id).status is RunStatus.COMPLETED
    assert e.calls == [] and e.esign.operations.records() == []
