"""M16b S1: approvals bind the referenced record's revision; the write checks it atomically."""

import multiprocessing
import sqlite3
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.flow import Clock
from tests.helpers import signer
from tests.unit.test_write_readback import BOB, CRM, IAM, SIGN, TOOLS, Env

from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, load_app_rules
from gawe.apps import contract_review, it_access
from gawe.apps.contract_review.record_preparation import ContractRecordPreparer
from gawe.apps.it_access.grant_preparation import GrantPreparer, validate_lifetime
from gawe.connectors.base import ConnectorError, Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.core.executor import ToolCall
from gawe.core.preparation import PreparationError
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.llm.fake import FakeProvider

SEED = contract_review.HERE / "samples/clm_seed.json"
IAM_SEED = it_access.HERE / "samples/iam_seed.json"
# Proposals that need an approval, so there is a gap between approval and execution.
GATED = {
    "crm": {**CRM, "contract_id": "C-1002", "risk_level": "low"},  # needs legal review
    "esign": SIGN,  # every signature request needs legal review
    "iam": {**IAM, "level": "admin"},  # admin access needs security review
}
# A field each record has, for "changed by someone else".
FIELD = {"crm": "status", "esign": "status", "iam": "department"}


def record_of(e: Env) -> str:
    """The contract id, or the account email, that the write step refers to."""
    args = e.steps[0]["args"]
    return str(args["email"] if e.system == "iam" else args["contract_id"])


def change_record(e: Env, fields: dict[str, Any]) -> str:
    if e.system == "iam":
        return e.iam.replace_user(record_of(e), fields)
    return e.clm.replace_contract(record_of(e), fields)


def read_record(e: Env) -> dict[str, Any]:
    if e.system == "iam":
        return e.iam.lookup_user(record_of(e))
    return e.clm.get_contract(record_of(e))


def current_revision(e: Env) -> str:
    if e.system == "iam":
        return e.iam.user_version(record_of(e))
    return e.clm.contract_version(record_of(e))


def bound_revision(e: Env) -> str:
    args = e.stores.steps.get(e.run_id, "write").resolved_args
    return str(next(iter(args["record"].values())))


def assert_record_changed(e: Env) -> None:
    step = e.stores.steps.get(e.run_id, "write")
    assert step.status is StepStatus.FAILED and step.error == "record_changed"
    assert e.stores.runs.get(e.run_id).status is RunStatus.FAILED
    assert e.target.records() == []  # nothing accepted
    assert len(e.calls) == 1  # refused once, never retried
    assert e.job.result["outcome"] == "not_dispatched"  # proven, no read-back wait


# -- the binding --------------------------------------------------------------------------


@pytest.mark.parametrize("system", ["crm", "esign", "iam"])
def test_preparation_binds_the_current_revision_into_the_hashed_arguments(
    tmp_path: Path, system: str
) -> None:
    e = Env(tmp_path, system)
    assert e.execute() is RunStatus.COMPLETED
    step = e.stores.steps.get(e.run_id, "write")
    (field,) = step.resolved_args["record"]
    assert e.target.records()[0]["record"] == step.resolved_args["record"]
    if system == "crm":  # M26: the rating write moves the revision and records the one it left
        assert e.target.records()[0]["result_version"] == current_revision(e)
        assert step.resolved_args["record"] != {field: current_revision(e)}
    else:
        assert step.resolved_args["record"] == {field: current_revision(e)}
    if system == "iam":
        assert step.resolved_args["expires_at"] == "2026-01-08T12:00:00Z"
        assert step.resolved_args["lifetime_hours"] == 168
        assert e.target.records()[0]["run_id"] == e.run_id


@pytest.mark.parametrize("system", ["crm", "esign", "iam"])
def test_a_record_replaced_between_approval_and_execution_is_refused(
    tmp_path: Path, system: str
) -> None:
    e = Env(tmp_path, system, args=GATED[system])
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    change_record(e, {FIELD[system]: "changed by someone else"})
    e.approve()
    e.advance()
    assert_record_changed(e)


@pytest.mark.parametrize("system", ["crm", "esign", "iam"])
def test_a_change_between_a_separate_read_and_the_write_is_refused(
    tmp_path: Path, system: str
) -> None:
    e = Env(tmp_path, system)
    hook_owner = e.iam if system == "iam" else e.clm
    hook_owner.before_guarded_write = lambda: change_record(e, {FIELD[system]: "raced"})
    assert e.execute() is RunStatus.FAILED
    assert_record_changed(e)


@pytest.mark.parametrize("system", ["crm", "esign", "iam"])
def test_a_change_away_and_back_is_still_refused(tmp_path: Path, system: str) -> None:
    """A revision, not a content hash: restoring the old content does not restore approval."""
    e = Env(tmp_path, system, args=GATED[system])
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    original = read_record(e)
    before = current_revision(e)
    change_record(e, {FIELD[system]: "briefly different"})
    change_record(e, {FIELD[system]: original[FIELD[system]]})
    after = read_record(e)
    assert current_revision(e) != before
    assert after == original  # same content, different revision
    e.approve()
    e.advance()
    assert_record_changed(e)


def test_esign_deal_value_is_checked_inside_the_write(tmp_path: Path) -> None:
    e = Env(tmp_path, "esign", args={**SIGN, "deal_value": 1})
    e.execute()
    step = e.stores.steps.get(e.run_id, "write")
    assert step.status is StepStatus.FAILED and step.error == "deal_value_mismatch"
    assert e.target.records() == []


def test_esign_makes_no_separate_read_before_the_write(tmp_path: Path) -> None:
    e = Env(tmp_path, "esign")
    e.clm.faults.fail_next(1, transient=False)  # would reject any get_contract call
    assert e.execute() is RunStatus.COMPLETED
    # The queued fault is still there: the write never called get_contract.
    with pytest.raises(ConnectorError, match="simulated_rejection"):
        e.clm.get_contract("C-1001")


# -- model input can never supply the binding --------------------------------------------


@pytest.mark.parametrize(
    "system,extra",
    [
        ("crm", {"record": {"contract_version": "attacker"}}),
        ("esign", {"record": {"contract_version": "attacker"}}),
        ("iam", {"record": {"user_version": "attacker"}}),
        ("iam", {"expires_at": "2099-01-01T00:00:00Z"}),
        ("iam", {"lifetime_hours": 1}),
    ],
)
def test_a_model_supplied_binding_never_reaches_policy(
    tmp_path: Path, system: str, extra: dict[str, Any]
) -> None:
    """The proposal schema has no such field, so the plan is refused before any step exists."""
    e = Env(tmp_path, system, args={**GATED[system], **extra}, plan_attempts=2)
    assert e.advance() is RunStatus.FAILED
    run = e.stores.runs.get(e.run_id)
    assert run.plan is None and run.decisions == {} and e.calls == []


def test_a_binding_supplied_through_a_reference_fails_before_policy(tmp_path: Path) -> None:
    """A reference that resolves to an object cannot smuggle fields past the proposal schema."""
    steps = [
        {"id": "lookup", "tool": "clm.get_contract", "args": {"contract_id": "C-1001"}},
        {
            "id": "write",
            "tool": TOOLS["crm"],
            "args": {**CRM, "summary": {"$ref": "lookup.output"}},
            "depends_on": ["lookup"],
        },
    ]
    e = Env(tmp_path, "crm", steps=steps)
    assert e.advance() is RunStatus.FAILED
    step = e.stores.steps.get(e.run_id, "write")
    assert step.status is StepStatus.FAILED and step.error == "preparation_invalid_proposal"
    assert step.policy_decision is None and e.calls == []


def test_a_top_level_reference_named_record_is_refused_at_planning(tmp_path: Path) -> None:
    steps = [
        {"id": "lookup", "tool": "clm.get_contract", "args": {"contract_id": "C-1001"}},
        {
            "id": "write",
            "tool": TOOLS["crm"],
            "args": {**CRM, "record": {"$ref": "lookup.output"}},
            "depends_on": ["lookup"],
        },
    ]
    e = Env(tmp_path, "crm", steps=steps, plan_attempts=2)
    assert e.advance() is RunStatus.FAILED
    assert e.stores.runs.get(e.run_id).plan is None and e.calls == []


# -- missing or unreadable records ---------------------------------------------------------


def test_an_unknown_contract_fails_in_preparation(tmp_path: Path) -> None:
    e = Env(tmp_path, "crm", args={**CRM, "contract_id": "C-9999"})
    e.advance()
    step = e.stores.steps.get(e.run_id, "write")
    assert step.status is StepStatus.FAILED and step.error == "contract_not_found"
    assert step.policy_decision is None and e.calls == []


def test_an_unknown_user_fails_in_preparation(tmp_path: Path) -> None:
    e = Env(tmp_path, "iam", args={**IAM, "email": "nobody@acme.example"})
    e.advance()
    step = e.stores.steps.get(e.run_id, "write")
    assert step.status is StepStatus.FAILED and step.error == "user_not_found"
    assert step.policy_decision is None and e.calls == []


class _Unreadable:
    def contract_version(self, contract_id: str) -> Any:
        raise OSError("contract system unavailable")

    def user_version(self, email: str) -> Any:
        return ""


def test_an_unreadable_or_empty_revision_fails_closed() -> None:
    with pytest.raises(PreparationError, match="record_version_unavailable"):
        ContractRecordPreparer(_Unreadable())(CRM, reapproval=False)  # type: ignore[arg-type]
    preparer = GrantPreparer(_Unreadable(), lifetime_hours=168, clock=Clock())  # type: ignore[arg-type]
    with pytest.raises(PreparationError, match="record_version_unavailable"):
        preparer(IAM, reapproval=False)


@pytest.mark.parametrize("hours", [0, 721, -1, 1.5, True, "168"])
def test_a_grant_lifetime_outside_one_hour_to_thirty_days_fails_startup(hours: Any) -> None:
    with pytest.raises(ValueError, match="grant lifetime"):
        validate_lifetime(hours)
    with pytest.raises(ValueError, match="grant lifetime"):
        it_access.bundle(Path("/nonexistent-never-written"), grant_lifetime_hours=hours)


def test_grant_lifetime_bounds_are_inclusive() -> None:
    assert validate_lifetime(1) == 1 and validate_lifetime(720) == 720


# -- startup checks ------------------------------------------------------------------------


@pytest.mark.parametrize("app", [contract_review, it_access])
def test_a_bound_tool_without_a_conditional_write_fails_startup(tmp_path: Path, app: Any) -> None:
    bundle = app.bundle(tmp_path)
    # Retired versions are never sent, so they rely on nothing (M22).
    bound = [(m.name, m.version) for m in bundle.manifests if m.record_binding and not m.retired]
    assert bound
    for identity in bound:
        without = {
            **bundle.guarantees,
            identity: bundle.guarantees[identity] - {"conditional_write"},
        }
        with pytest.raises(ValueError, match="conditional_write"):
            load_app_rules(replace(bundle, guarantees=without))


@pytest.mark.parametrize("app", [contract_review, it_access])
def test_a_bound_tool_without_a_preparer_fails_startup(tmp_path: Path, app: Any) -> None:
    bundle = app.bundle(tmp_path)
    bound = [m for m in bundle.manifests if m.record_binding]
    assert bound
    for manifest in bound:
        preparers = dict(bundle.preparers)
        del preparers[(manifest.name, manifest.version)]
        with pytest.raises(ValueError, match="preparation adapters"):
            load_app_rules(replace(bundle, preparers=preparers))
        unprepared = manifest.model_copy(update={"preparation_required": False})
        manifests = [unprepared if m is manifest else m for m in bundle.manifests]
        with pytest.raises(ValueError, match="trusted preparation"):
            load_app_rules(replace(bundle, manifests=manifests, preparers=preparers))


def test_every_shipped_write_that_names_a_record_is_bound() -> None:
    for app in (contract_review, it_access):
        current = [m for m in app.MANIFESTS if not m.retired and m.side_effect_class != "read"]
        bound = {m.name for m in current if m.record_binding}
        assert bound == (
            {"iam.grant_access"}
            if app is it_access
            else {
                "crm.write_assessment",
                "crm.restore_assessment",
                "esign.send_for_signature",
            }
        )


# -- concurrency across processes ---------------------------------------------------------


def _hold_a_change(directory: str, started: Any, release_after: float) -> None:
    """Another process changes the contract and holds its transaction open for a while."""
    conn = sqlite3.connect(Path(directory) / "clm-records.sqlite3", timeout=10)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE contracts SET revision='other-process' WHERE record_id='C-1001'")
    started.set()
    time.sleep(release_after)
    conn.commit()
    conn.close()


def _guarded_write_holding(directory: str, revision: str, checked: Any, results: Any) -> None:
    """Another process passes the revision check and holds its transaction open a while."""
    clm = MockCLM.from_seed(SEED, Ledger(Path(directory) / "ledger.jsonl"))
    original = clm.contracts.guard

    def slow_guard(*args: Any, **kwargs: Any) -> Any:
        check = original(*args, **kwargs)

        def run(conn: sqlite3.Connection) -> None:
            check(conn)
            checked.set()
            time.sleep(0.5)

        return run

    clm.contracts.guard = slow_guard  # type: ignore[method-assign]
    try:
        clm.write_assessment("key-b", "C-1001", "low", "s", contract_version=revision)
        results.put("accepted")
    except ConnectorError as exc:
        results.put(exc.code)


def test_a_change_committed_by_another_process_is_seen_by_the_guard(tmp_path: Path) -> None:
    clm = MockCLM.from_seed(SEED, Ledger(tmp_path / "ledger.jsonl"))
    stale = clm.contract_version("C-1001")
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    other = ctx.Process(target=_hold_a_change, args=(str(tmp_path), started, 0.5))
    other.start()
    assert started.wait(10)
    # The other process holds the write lock; this write waits for it, then sees its change.
    with pytest.raises(ConnectorError, match="record_changed"):
        clm.write_assessment("key-a", "C-1001", "low", "s", contract_version=stale)
    other.join(10)
    assert other.exitcode == 0 and clm.operations.records() == []


def test_a_change_cannot_land_between_another_process_check_and_its_write(
    tmp_path: Path,
) -> None:
    clm = MockCLM.from_seed(SEED, Ledger(tmp_path / "ledger.jsonl"))
    revision = clm.contract_version("C-1001")
    ctx = multiprocessing.get_context("spawn")
    checked, results = ctx.Event(), ctx.Queue()
    writer = ctx.Process(
        target=_guarded_write_holding, args=(str(tmp_path), revision, checked, results)
    )
    writer.start()
    assert checked.wait(10)
    # The writer has checked but not committed; this change must wait for it to finish.
    changed = clm.replace_contract("C-1001", {"status": "changed"})
    assert results.get(timeout=10) == "accepted"
    writer.join(10)
    (accepted,) = clm.operations.records()
    assert accepted["record"] == {"contract_version": revision} and changed != revision
    # The accepted write was checked against the revision that was current when it committed.
    assert clm.contract_version("C-1001") == changed


# -- retired 1.0.0 steps -------------------------------------------------------------------


def _legacy_bundle(e: Env, system: str) -> Any:
    """The pre-M16b app: the 1.0.0 write was the executable version."""
    name = TOOLS[system]
    v1 = next(m for m in e.bundle.manifests if m.name == name and m.version == "1.0.0")
    later = {(m.name, m.version) for m in e.bundle.manifests if m.name == name and m is not v1}
    manifests = [
        m.model_copy(update={"retired": False}) if m is v1 else m
        for m in e.bundle.manifests
        if (m.name, m.version) not in later
    ]
    preparers = {k: v for k, v in e.bundle.preparers.items() if k not in later}
    readbacks = {k: v for k, v in e.bundle.readbacks.items() if k not in later}
    # Before M16b the 1.0.0 write was the current one: idempotent and read back, not conditional.
    guarantees = {k: v for k, v in e.bundle.guarantees.items() if k not in later}
    revokers = {k: v for k, v in e.bundle.revokers.items() if k[0] != name}  # none before M16b

    def legacy_write(args: Any, call: ToolCall) -> Any:
        e.calls.append(call)
        ops = {"crm": e.clm, "esign": e.esign, "iam": e.iam}[system].legacy_operations
        ops.accept(call.idempotency_key, {**args, **_legacy_ids(system, call)})
        return _legacy_ids(system, call) | {k: args[k] for k in _legacy_echo(system)}

    policy = e.bundle.policy_rules.read_text(encoding="utf-8")
    marker = "  # Every grant expires."
    path = e.ledger_dir / "pre-m16b-policies.yaml"
    path.write_text(policy.split(marker)[0], encoding="utf-8")  # the pre-M16b rules
    return replace(
        e.bundle,
        manifests=manifests,
        preparers=preparers,
        action_variables={k: v for k, v in e.bundle.action_variables.items() if k not in later},
        readbacks=readbacks,
        guarantees=guarantees,
        dispatch_conditions={  # none before M22, and none for the 1.0.0 write
            k: v for k, v in e.bundle.dispatch_conditions.items() if k[0] != name
        },
        revokers=revokers,
        undoable={k: v for k, v in e.bundle.undoable.items() if k not in later},
        policy_rules=path,
        tools={**e.bundle.tools, name: legacy_write},
    )


def _legacy_ids(system: str, call: ToolCall) -> dict[str, str]:
    key = call.idempotency_key[:12]
    return {
        "crm": {"assessment_id": f"as-{key}"},
        "esign": {"envelope_id": f"env-{key}"},
        "iam": {"grant_id": f"gr-{key}"},
    }[system]


def _legacy_echo(system: str) -> tuple[str, ...]:
    return {
        "crm": ("contract_id", "risk_level"),
        "esign": ("contract_id",),
        "iam": ("email", "system", "level"),
    }[system]


def _service(e: Env, bundle: Any) -> Any:
    return assemble(
        bundle,
        stores=e.stores,
        signer=signer(),
        provider=FakeProvider([{"steps": e.steps}]),
        background=InlineBackground(),
        clock=e.clock,
    )


@pytest.mark.parametrize("system", ["crm", "esign", "iam"])
def test_an_unexecuted_v1_step_fails_closed_after_the_upgrade(tmp_path: Path, system: str) -> None:
    e = Env(tmp_path, system, args=GATED[system])
    old = _service(e, _legacy_bundle(e, system))
    old._orch.advance(e.run_id)
    step = e.stores.steps.get(e.run_id, "write")
    assert step.tool_version == "1.0.0" and step.status is StepStatus.AWAITING_APPROVAL
    new = _service(e, e.bundle)
    new._orch.advance(e.run_id)
    step = e.stores.steps.get(e.run_id, "write")
    assert step.status is StepStatus.FAILED and step.error == "unsupported_tool_version"
    assert step.tool_version == "1.0.0"  # never relabelled
    assert e.calls == []


@pytest.mark.parametrize("system", ["crm", "esign", "iam"])
def test_a_dispatched_v1_write_still_verifies_after_the_upgrade(
    tmp_path: Path, system: str
) -> None:
    e = Env(tmp_path, system)
    {"crm": e.clm, "esign": e.esign, "iam": e.iam}[system].legacy_operations.visible_at = (
        e.clock.now + timedelta(seconds=60)
    )
    old = _service(e, _legacy_bundle(e, system))
    old._orch.advance(e.run_id)
    if old.inbox(BOB):
        (item,) = old.inbox(BOB)
        old.decide(BOB, item.approval_id, decision="approved", action_hash=item.action_hash)
    step = e.stores.steps.get(e.run_id, "write")
    assert step.status is StepStatus.AWAITING_READBACK and len(e.calls) == 1
    e.clock.advance(seconds=60)
    new = _service(e, e.bundle)
    new.recover_interrupted()
    new._orch.advance(e.run_id)
    step = e.stores.steps.get(e.run_id, "write")
    assert step.status is StepStatus.SUCCEEDED and step.tool_version == "1.0.0"
    assert len(e.calls) == 1  # verified, not resent
