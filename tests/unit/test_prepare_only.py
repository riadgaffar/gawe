"""M20: an action class the engine only prepares is never dispatched, by any path (S1), and is
handed to a person instead (S2)."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_write_readback import ALICE, BOB, CRM, Env

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, load_app_rules
from gawe.apps import contract_review
from gawe.core.approvals import Principal
from gawe.core.executor import ExecutionRefusedError
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.engine.orchestrator import HandoffError
from gawe.llm.fake import FakeProvider

CRM_WRITE = ("crm.write_assessment", "2.0.0")
SIGNATURE = ("esign.send_for_signature", "2.0.0")


def prepare_only(e: Env, *classes: tuple[str, str]) -> None:
    """Restart the service over the same stores and signer with these classes prepare-only, as
    a deployment that turned the setting on would. Nothing stored on the run changes."""
    signer = e.orch._executor._signer
    e.bundle = replace(e.bundle, prepare_only={c: "legal_reviewer" for c in classes})
    e.service = assemble(
        e.bundle,
        stores=e.stores,
        signer=signer,
        provider=FakeProvider([{"steps": e.steps}]),  # the plan, if not made yet
        background=InlineBackground(),
        clock=e.clock,
    )
    e.orch = e.service._orch


def write_step(e: Env) -> Any:
    return e.stores.steps.get(e.run_id, "write")


def assert_refused(e: Env) -> None:
    """Reached the executor anyway (an approval from before the setting): refused there."""
    step = write_step(e)
    assert step.status is StepStatus.FAILED and step.error == "refused_prepare_only"
    assert e.calls == [] and e.target.records() == []


def assert_handed_off(e: Env) -> None:
    step = write_step(e)
    assert step.status is StepStatus.PREPARED and step.handoff is not None
    assert e.stores.runs.get(e.run_id).status is RunStatus.WAITING_FOR_PERSON
    assert e.calls == [] and e.target.records() == []


# --- every path to dispatch ---------------------------------------------------------------------


def test_an_allowing_rule_does_not_dispatch(tmp_path: Path) -> None:
    e = Env(tmp_path, "crm")
    prepare_only(e, CRM_WRITE)
    assert e.advance() is RunStatus.WAITING_FOR_PERSON
    assert_handed_off(e)


def test_a_step_that_needs_approval_asks_none_and_is_handed_off(tmp_path: Path) -> None:
    # Policy still judges it (and records needs_approval), but no approval is asked: a yes could
    # never let the engine send it.
    e = Env(tmp_path, "esign")
    prepare_only(e, SIGNATURE)
    e.advance()
    assert e.service.inbox(BOB) == []
    assert e.stores.runs.get(e.run_id).decisions["write"].outcome == "needs_approval"
    assert_handed_off(e)


def test_an_approval_granted_before_the_setting_does_not_dispatch(tmp_path: Path) -> None:
    e = Env(tmp_path, "esign")
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    (request,) = e.service.inbox(BOB)
    e.service._approvals.decide(  # approved under the old configuration, without advancing
        request.approval_id,
        decider=Principal(BOB.user_id, BOB.roles),
        decision="approved",
        action_hash=request.action_hash,
    )
    prepare_only(e, SIGNATURE)
    e.advance()
    assert_refused(e)


def test_a_resumed_run_does_not_dispatch(tmp_path: Path) -> None:
    e = Env(tmp_path, "esign")
    prepare_only(e, SIGNATURE)
    e.advance()
    e.service.resume_run(ALICE, e.run_id)
    e.advance()
    assert_handed_off(e)


def test_retries_and_sweeps_never_dispatch(tmp_path: Path) -> None:
    e = Env(tmp_path, "crm")
    prepare_only(e, CRM_WRITE)
    e.advance()
    for _ in range(3):
        e.clock.advance(minutes=10)
        e.advance()
        assert e.service.maintenance_runner is not None
        e.service.maintenance_runner.tick()
    assert_handed_off(e)


def test_a_direct_executor_call_with_a_valid_allow_is_refused(tmp_path: Path) -> None:
    e = Env(tmp_path, "crm")
    prepare_only(e, CRM_WRITE)
    e.advance()
    step = write_step(e)
    decision = e.stores.runs.get(e.run_id).decisions["write"]
    assert decision.outcome == "allow"  # signed and valid for this action
    with pytest.raises(ExecutionRefusedError, match="prepare_only"):
        e.orch._executor.execute(e.run_id, step.step_id, decision)
    assert e.calls == []


def test_recovery_of_a_write_dispatched_before_the_setting_only_reads_back(tmp_path: Path) -> None:
    # The write ran before the setting existed; turning it on later must not dispatch again, and
    # reading back what already happened is not a dispatch.
    e = Env(tmp_path, "crm")
    e.target.visible_at = e.clock.now.replace(year=e.clock.now.year + 1)  # reply not yet readable
    e.advance()
    assert len(e.calls) == 1
    prepare_only(e, CRM_WRITE)
    for _ in range(3):
        e.clock.advance(minutes=1)
        e.advance()
    assert len(e.calls) == 1


# --- what doesn't change ----------------------------------------------------------------------


def test_a_tool_outside_the_setting_runs_as_before(tmp_path: Path) -> None:
    e = Env(tmp_path, "crm")
    prepare_only(e, SIGNATURE)
    assert e.advance() is RunStatus.COMPLETED and len(e.calls) == 1


def test_the_shipped_apps_prepare_nothing(tmp_path: Path) -> None:
    from gawe.apps import it_access

    assert contract_review.bundle(tmp_path / "cr").prepare_only == {}
    assert it_access.bundle(tmp_path / "it").prepare_only == {}


def test_the_demo_variant_makes_only_signatures_prepare_only(tmp_path: Path) -> None:
    demo = contract_review.signatures_by_hand(tmp_path)
    assert demo.prepare_only == {SIGNATURE: "legal_reviewer"}
    load_app_rules(demo)  # passes the startup check
    assert replace(demo, prepare_only={}).manifests == contract_review.bundle(tmp_path).manifests


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ({("crm.write_assessment", "9.9.9"): "legal_reviewer"}, "doesn't register"),
        ({("clm.get_contract", "1.0.0"): "legal_reviewer"}, "a read"),
        ({CRM_WRITE: "nobody_has_this_role"}, "unknown role"),
    ],
)
def test_a_bad_setting_stops_startup(tmp_path: Path, setting: dict, message: str) -> None:
    bundle = replace(contract_review.bundle(tmp_path), prepare_only=setting)
    with pytest.raises(ValueError, match=message):
        load_app_rules(bundle)


@pytest.mark.parametrize("boundary", ["intent", "attempt", "accept"])
def test_recovery_after_a_crash_never_dispatches_again_once_prepare_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    # A crash at a dispatch boundary under the old setting, then recovery under the new one: the
    # engine may only read back what already happened, never send again.
    from tests.flow import Crash

    e = Env(tmp_path, "crm")
    resolver = e.orch._executor.verification
    assert resolver is not None
    target: Any
    name: str
    if boundary == "intent":
        target, name = resolver.jobs, "add"
    elif boundary == "attempt":
        target, name = e.stores.executions, "start"
    else:
        target, name = e.target, "accept"
    original = getattr(target, name)

    def crash_after(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(target, name, crash_after)
    with pytest.raises(Crash):
        e.advance()
    calls_at_crash = len(e.calls)
    monkeypatch.undo()
    prepare_only(e, CRM_WRITE)
    for _ in range(3):
        e.clock.advance(minutes=6)
        e.advance()
    assert len(e.calls) == calls_at_crash


# --- S2: the hand-off --------------------------------------------------------------------------


def handed_off(tmp_path: Path, steps: list[dict[str, Any]] | None = None) -> Env:
    e = Env(tmp_path, "crm", steps=steps)
    prepare_only(e, CRM_WRITE)
    e.advance()
    return e


def mark(e: Env, who: Identity = BOB, *, done: bool = True, **kwargs: Any) -> Any:
    step = write_step(e)
    return e.orch.mark_handoff(
        e.run_id,
        "write",
        user_id=who.user_id,
        roles=who.roles,
        done=done,
        action_hash=kwargs.pop("action_hash", step.action_hash),
        **kwargs,
    )


def test_done_completes_the_run_and_records_who_and_the_reference(tmp_path: Path) -> None:
    e = handed_off(tmp_path)
    marked = mark(e, reference="ENV-2041")
    assert marked.status is StepStatus.DONE_BY_PERSON
    assert (marked.handoff.by, marked.handoff.reference) == (BOB.user_id, "ENV-2041")
    assert e.advance() is RunStatus.COMPLETED and e.calls == []


def test_not_done_ends_the_run_like_a_rejection(tmp_path: Path) -> None:
    e = handed_off(tmp_path)
    assert mark(e, done=False).status is StepStatus.NOT_DONE_BY_PERSON
    run = e.orch.advance(e.run_id)
    assert run.status is RunStatus.DENIED and run.error == "handoff_not_done"


@pytest.mark.parametrize(
    ("who", "kwargs", "code"),
    [
        (Identity("t1", "carol", frozenset({"case_manager"})), {}, "role_required"),
        (Identity("t1", ALICE.user_id, frozenset({"legal_reviewer"})), {}, "requester_cannot_mark"),
        (BOB, {"action_hash": "0" * 64}, "action_hash_mismatch"),
        (BOB, {"reference": "has spaces and; punctuation"}, "reference_invalid"),
        (BOB, {"reference": "x" * 121}, "reference_invalid"),
    ],
)
def test_a_mark_is_refused_unless_it_is_the_roles_holder_with_the_current_hash(
    tmp_path: Path, who: Identity, kwargs: dict[str, Any], code: str
) -> None:
    e = handed_off(tmp_path)
    with pytest.raises(HandoffError, match=code):
        mark(e, who, **kwargs)
    assert write_step(e).status is StepStatus.PREPARED


def test_a_hand_off_is_marked_once(tmp_path: Path) -> None:
    e = handed_off(tmp_path)
    mark(e)
    with pytest.raises(HandoffError, match="not_waiting_for_a_person"):
        mark(e, done=False)


DOWNSTREAM = {
    "id": "next",
    "tool": "clm.get_contract",
    "args": {"contract_id": "C-1001"},
    "depends_on": ["write"],
}
WRITE = {"id": "write", "tool": "crm.write_assessment", "args": dict(CRM), "depends_on": []}


def test_a_dependant_waits_for_done_then_runs(tmp_path: Path) -> None:
    e = handed_off(tmp_path, [WRITE, DOWNSTREAM])
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.PENDING
    mark(e)
    assert e.advance() is RunStatus.COMPLETED
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.SUCCEEDED


def test_a_dependant_fails_when_the_person_did_not_do_it(tmp_path: Path) -> None:
    e = handed_off(tmp_path, [WRITE, DOWNSTREAM])
    mark(e, done=False)
    assert e.advance() is RunStatus.DENIED
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.FAILED


def test_nothing_can_use_a_hand_offs_output(tmp_path: Path) -> None:
    uses_output = {
        **DOWNSTREAM,
        "args": {"contract_id": {"$ref": "write.output.contract_id"}},
    }
    e = handed_off(tmp_path, [WRITE, uses_output])
    mark(e)
    assert e.advance() is RunStatus.FAILED
    assert e.stores.steps.get(e.run_id, "next").error == "handoff_has_no_output"


def test_audit_carries_hashes_and_codes_never_arguments_or_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The audit store keeps only a hash of each payload, so check what is handed to it.
    e = Env(tmp_path, "crm")
    prepare_only(e, CRM_WRITE)
    seen: list[tuple[str, str, dict[str, Any]]] = []
    original = e.orch._audit.record

    def spy(run_id: str, kind: str, actor: str, payload: dict[str, Any]) -> Any:
        seen.append((kind, actor, payload))
        return original(run_id, kind, actor, payload)

    monkeypatch.setattr(e.orch._audit, "record", spy)
    e.advance()
    mark(e, reference="ENV-2041")
    events = {kind: (actor, payload) for kind, actor, payload in seen}
    _, prepared = events["handoff.prepared"]
    actor, done = events["handoff.done"]
    assert prepared["role"] == "legal_reviewer" and prepared["policy_outcome"] == "allow"
    assert actor == BOB.user_id and done["reference_chars"] == 8
    text = repr([payload for _, _, payload in seen])
    assert "Private review text" not in text and "ENV-2041" not in text


def test_a_crash_after_preparing_neither_dispatches_nor_hands_off_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.flow import Crash

    e = Env(tmp_path, "crm")
    prepare_only(e, CRM_WRITE)
    original = e.orch._audit.record

    def crash_after_prepared(run_id: str, kind: str, *args: Any, **kwargs: Any) -> Any:
        value = original(run_id, kind, *args, **kwargs)
        if kind == "handoff.prepared":
            raise Crash()
        return value

    monkeypatch.setattr(e.orch._audit, "record", crash_after_prepared)
    with pytest.raises(Crash):
        e.advance()
    monkeypatch.undo()
    e.advance()
    e.advance()
    kinds = [ev.type for ev in e.service._audit.events(e.run_id)]
    assert kinds.count("handoff.prepared") == 1 and e.calls == []
    mark(e)
    assert e.advance() is RunStatus.COMPLETED


def test_cancelling_a_waiting_run_closes_its_hand_off(tmp_path: Path) -> None:
    e = handed_off(tmp_path)
    e.service.cancel_run(ALICE, e.run_id)
    assert write_step(e).status is StepStatus.FAILED
    with pytest.raises(HandoffError, match="not_waiting_for_a_person"):
        mark(e)
