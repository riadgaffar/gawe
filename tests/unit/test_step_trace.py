"""M23 S3: a step traced from proposal to result.

One run per outcome, each with no gaps: success, policy denial, a rejected approval, an unknown
outcome, a refused condition, a hand-off marked done, and a recipient change that superseded two
earlier hashes. Then planted mismatches, which the trace must name; the tenant boundary; and no
argument value anywhere in the trace.
"""

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.helpers import signer
from tests.unit.test_trusted_submitter import (
    AGENT,
    DANA,
    LEE,
    SAM,
    SECURITY,
    Desk,
    findings,
    grant_plan,
)

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.errors import NotFoundError
from gawe.api.schemas import StepTraceView
from gawe.api.wiring import assemble, memory_stores
from gawe.apps import it_access as ia
from gawe.connectors.mock_tickets import MockTickets
from gawe.core.approvals import ApprovalRecord
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.records import Handoff
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.llm.fake import FakeProvider

GRANT = ("iam.grant_access", "3.0.0")
OTHER_HASH = "0" * 64


def trace(d: Any, run_id: str, step_id: str = "grant") -> StepTraceView:
    return d.service.step_trace(AGENT, run_id, step_id)


# --- one run per outcome, no gaps ---------------------------------------------------------------


def test_a_successful_grant_links_every_record(tmp_path: Path) -> None:
    d = Desk(tmp_path, [findings(DANA), grant_plan(DANA)])
    view = d.run(DANA)
    assert view.status == "completed"
    t = trace(d, view.run_id)
    assert t.gaps == ()
    assert t.action_hash == view.steps[-1].action_hash and t.args_hash is not None
    assert t.decision is not None and t.decision.outcome == "allow"
    assert t.decision.action_hash == t.action_hash
    assert t.decision.signature_valid is True and t.decision.audited is True
    assert t.approvals == ()
    (attempt,) = t.attempts
    assert attempt.status == "succeeded" and attempt.action_hash == t.action_hash
    assert [(c.result, c.count) for c in t.conditions] == [("held", 1)]
    (readback,) = t.readback
    assert readback.current and readback.outcome == "agreement"
    assert readback.operation_key == attempt.operation_key


def test_a_denied_grant_stops_at_the_decision(tmp_path: Path) -> None:
    d = Desk(tmp_path, [findings(DANA), grant_plan(SAM, "billing", "write")])
    view = d.run(DANA)
    assert view.status == "denied"
    t = trace(d, view.run_id)
    assert t.gaps == ()
    assert t.decision is not None and t.decision.outcome == "deny" and t.decision.audited
    assert "requester-mismatch-denied" in t.decision.rule_ids
    assert t.approvals == () and t.attempts == () and t.readback == () and t.conditions == ()


def test_a_rejected_approval_is_linked_and_nothing_is_dispatched(tmp_path: Path) -> None:
    d = Desk(tmp_path, [findings(SAM), grant_plan(SAM)])
    view = d.run(LEE)
    (item,) = d.service.inbox(SECURITY)
    d.service.decide(SECURITY, item.approval_id, decision="rejected", action_hash=item.action_hash)
    t = trace(d, view.run_id)
    assert t.gaps == ()
    assert t.decision is not None and t.decision.outcome == "needs_approval"
    (approval,) = t.approvals
    assert approval.current and approval.decision == "rejected" and approval.audited
    assert approval.decided_by == SECURITY.user_id
    assert t.attempts == () and t.readback == ()


def test_an_unknown_outcome_is_linked_to_its_read_back(tmp_path: Path) -> None:
    from tests.unit.test_write_readback import ALICE, Env

    e = Env(tmp_path)  # a CRM write, read back from the CRM's own operation record
    e.target.read_failures = 100
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    e.clock.now = e.job.deadline
    assert e.advance() is RunStatus.UNKNOWN
    t = e.service.step_trace(ALICE, e.run_id, "write")
    assert t.gaps == ()
    (attempt,) = t.attempts
    (readback,) = t.readback
    assert readback.current and readback.outcome == "unknown"
    assert readback.code == "verification_deadline"
    assert readback.operation_key == attempt.operation_key


def test_a_refused_condition_is_linked_by_its_check(tmp_path: Path) -> None:
    from tests.unit.test_dispatch_conditions import Desk as WaitingDesk

    d = WaitingDesk(tmp_path)  # Lee asks for Sam's access; it waits for security
    d.tickets.set_status("t1", "T-1", "closed")
    assert d.approve().status == "failed"
    t = d.service.step_trace(AGENT, d.run_id, "grant")
    assert t.gaps == ()
    assert [(c.result, c.count) for c in t.conditions] == [("changed:ticket_open", 1)]
    (attempt,) = t.attempts
    assert attempt.status == "failed" and attempt.error == "condition_changed"
    (readback,) = t.readback
    assert readback.outcome == "not_dispatched" and readback.code == "condition_changed"


def test_a_hand_off_marked_done_is_linked(tmp_path: Path) -> None:
    tickets = MockTickets.in_state_dir(tmp_path)
    bundle = replace(ia.bundle(tmp_path, tickets=tickets), prepare_only={GRANT: "security_admin"})
    service = assemble(
        bundle,
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider([findings(DANA), grant_plan(DANA)]),
        background=InlineBackground(),
    )
    tickets.create("t1", "T-1", submitted_by=DANA, channel="portal", body="ticket")
    run_id = service.start_run(
        AGENT, objective="Process the request", profile="it_access", source_id="T-1"
    ).run_id
    (handoff,) = service.handoffs(SECURITY)
    service.mark_handoff(
        SECURITY, run_id, "grant", done=True, action_hash=handoff.action_hash, reference="CHG-1"
    )
    t = service.step_trace(AGENT, run_id, "grant")
    assert t.gaps == ()
    assert t.status == "done_by_person"
    assert t.handoff is not None and t.handoff.marked_by == SECURITY.user_id
    assert t.handoff.reference == "CHG-1"
    assert t.attempts == () and t.readback == ()


def test_a_recipient_change_supersedes_earlier_hashes_without_a_gap(tmp_path: Path) -> None:
    from tests.unit.test_apps_contract_review import ALICE, BOB
    from tests.unit.test_prepared_email import world

    from gawe.connectors.mock_directory import DirectoryEntry

    w = world(tmp_path)
    assert w.email.server is not None
    directory = w.email.server.directory
    group = ("legal@acme.example", "vendor@outside.example")
    directory.put("mixed@acme.example", DirectoryEntry(kind="group", members=group))
    run_id = w.run(document=None)
    directory.put("mixed@acme.example", DirectoryEntry(kind="group", members=group[:1]))
    w.approve(BOB)  # the members changed since: refused at dispatch, approval asked again
    w.approve(BOB)
    assert w.status(run_id) == "completed"
    t = w.service.step_trace(ALICE, run_id, "mail")
    assert t.gaps == ()
    assert len(t.superseded_hashes) == 1 and t.action_hash not in t.superseded_hashes
    assert [a.current for a in t.approvals] == [False, True]
    assert {r.action_hash for r in t.readback} == {t.action_hash, *t.superseded_hashes}


# --- planted mismatches are named ---------------------------------------------------------------


def completed(tmp_path: Path) -> tuple[Any, str]:
    d = Desk(tmp_path, [findings(DANA), grant_plan(DANA)])
    view = d.run(DANA)
    assert view.status == "completed" and trace(d, view.run_id).gaps == ()
    return d, view.run_id


def test_an_approval_bound_to_another_hash_is_a_gap(tmp_path: Path) -> None:
    d, run_id = completed(tmp_path)
    (attempt,) = trace(d, run_id).attempts
    d.service._approval_store.add(
        ApprovalRecord(
            approval_id="planted",
            run_id=run_id,
            step_id="grant",
            action_hash=OTHER_HASH,
            approver_role="security_admin",
            requested_by="someone",
            requested_at=attempt.started_at,
            expires_at=attempt.finished_at,
        )
    )
    t = trace(d, run_id)
    assert "approval_hash_mismatch" in t.gaps
    assert "approval_not_audited" in t.gaps  # nor was it ever requested through GAWE


def test_a_read_back_job_for_another_hash_is_a_gap(tmp_path: Path) -> None:
    d, run_id = completed(tmp_path)
    store = d.service._maintenance
    (job,) = [j for j in store.for_run("t1", run_id) if j.kind == "write_readback"]
    store.add(job.model_copy(update={"job_id": "f" * 64, "action_hash": OTHER_HASH}))
    assert "readback_hash_mismatch" in trace(d, run_id).gaps


def test_a_missing_read_back_is_a_gap(tmp_path: Path) -> None:
    d, run_id = completed(tmp_path)
    d.service._maintenance = None
    assert "readback_missing" in trace(d, run_id).gaps


def test_a_decision_for_another_hash_or_with_a_bad_signature_is_a_gap(tmp_path: Path) -> None:
    d, run_id = completed(tmp_path)
    runs, steps = d.service._runs, d.service._steps
    step = steps.get(run_id, "grant")
    assert step.policy_decision is not None
    forged = step.policy_decision.model_copy(update={"action_hash": OTHER_HASH})
    steps._steps[(run_id, "grant")] = step.model_copy(update={"policy_decision": forged})
    gaps = trace(d, run_id).gaps
    assert {"decision_hash_mismatch", "decision_signature_invalid"} <= set(gaps)
    assert "decision_not_audited" in gaps
    assert runs.get(run_id).decisions["grant"].action_hash == step.action_hash


def test_an_attempt_keyed_by_another_hash_is_a_gap(tmp_path: Path) -> None:
    d, run_id = completed(tmp_path)
    executions = d.service._executions
    history = executions._attempts[(run_id, "grant")]
    history[0] = history[0].model_copy(update={"idempotency_key": OTHER_HASH})
    assert "attempt_key_mismatch" in trace(d, run_id).gaps


def test_a_missing_audit_event_is_a_gap(tmp_path: Path) -> None:
    d, run_id = completed(tmp_path)
    audit_store = d.service._audit._store
    events = audit_store._events[run_id]
    audit_store._events[run_id] = [e for e in events if e.type != "policy.decided"]
    assert "decision_not_audited" in trace(d, run_id).gaps


def set_step(d: Any, run_id: str, **changes: Any) -> None:
    steps = d.service._steps
    steps._steps[(run_id, "grant")] = steps.get(run_id, "grant").model_copy(update=changes)


def set_decision(d: Any, run_id: str, **changes: Any) -> None:
    decision = d.service._steps.get(run_id, "grant").policy_decision
    changed = decision.model_copy(update=changes)
    set_step(d, run_id, policy_decision=changed)
    d.service._runs.update(run_id, decisions={"grant": changed})


def add_job(d: Any, run_id: str, **changes: Any) -> None:
    store = d.service._maintenance
    (job,) = [j for j in store.for_run("t1", run_id) if j.kind == "write_readback"]
    assert store.add(job.model_copy(update={"job_id": "e" * 64, **changes}))


def drop_events(d: Any, run_id: str, kind: str) -> None:
    audit_store = d.service._audit._store
    audit_store._events[run_id] = [e for e in audit_store._events[run_id] if e.type != kind]


# Each gap code not covered above, planted on a completed automatic grant.
PLANTED = {
    "decision_missing": lambda d, r: (
        set_step(d, r, policy_decision=None),
        d.service._runs.update(r, decisions={}),
    ),
    "decision_for_other_step": lambda d, r: set_decision(d, r, step_id="other"),
    "dispatched_after_denial": lambda d, r: set_decision(d, r, outcome="deny"),
    "dispatched_without_approval": lambda d, r: set_decision(
        d, r, outcome="needs_approval", approver_role="security_admin"
    ),
    "dispatched_after_handoff": lambda d, r: set_step(d, r, handoff=Handoff(role="security_admin")),
    "condition_not_checked": lambda d, r: drop_events(d, r, "condition.checked"),
    "handoff_missing": lambda d, r: set_step(d, r, status=StepStatus.DONE_BY_PERSON),
    "readback_plan_mismatch": lambda d, r: add_job(d, r, plan_version=2),
    "readback_key_mismatch": lambda d, r: add_job(d, r, operation_key="k" * 64),
}


@pytest.mark.parametrize("code", sorted(PLANTED))
def test_each_planted_break_is_named(tmp_path: Path, code: str) -> None:
    d, run_id = completed(tmp_path)
    PLANTED[code](d, run_id)
    assert code in trace(d, run_id).gaps


def test_a_missing_approval_is_a_gap(tmp_path: Path) -> None:
    d = Desk(tmp_path, [findings(SAM), grant_plan(SAM)])
    view = d.run(LEE)  # waits for security
    assert trace(d, view.run_id).gaps == ()
    d.service._approval_store._records.clear()
    assert trace(d, view.run_id).gaps == ("approval_missing",)


def test_every_gap_code_is_planted_somewhere() -> None:
    import inspect

    from gawe.api import trace as module

    codes = set(re.findall(r'gaps\.append\("([a-z_]+)"\)', inspect.getsource(module)))
    here = Path(__file__).read_text(encoding="utf-8")
    assert codes and not [c for c in codes if f'"{c}"' not in here]


# --- boundaries ---------------------------------------------------------------------------------


def test_another_tenants_run_or_an_unplanned_step_is_not_found(tmp_path: Path) -> None:
    d, run_id = completed(tmp_path)
    stranger = Identity("t2", "ivy", frozenset({"it_agent"}))
    with pytest.raises(NotFoundError):
        d.service.step_trace(stranger, run_id, "grant")
    with pytest.raises(NotFoundError):
        d.service.step_trace(AGENT, run_id, "nope")


def test_the_trace_carries_no_argument_value(tmp_path: Path) -> None:
    d, run_id = completed(tmp_path)
    step = d.service._steps.get(run_id, "grant")
    shown = trace(d, run_id).model_dump(mode="json")

    def leaves(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set().union(*(leaves(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(leaves(v) for v in value))
        return {value} if isinstance(value, str) else set()

    values = leaves(step.resolved_args) | leaves(step.preparation)
    assert DANA in values
    assert not values & leaves(shown)  # no argument or preparation value is a value of the trace
    assert shown["args_hash"] == sha256_hex(canonical_json(step.resolved_args))
