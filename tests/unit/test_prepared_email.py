"""Email v2 binds authorization to the independently accepted envelope."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_apps_contract_review import ALICE, BOB, World, step

from gawe.api.wiring import load_app_rules
from gawe.apps import contract_review
from gawe.connectors.mock_directory import DirectoryEntry
from gawe.core.canonical import action_hash
from gawe.core.policy.rules import PolicyEvaluationError, apply_operator
from gawe.core.statemachine import StepStatus


def world(path: Path, to: str = "mixed@acme.example") -> World:
    return World(
        path,
        [
            {
                "steps": [
                    step(
                        "mail",
                        "email.send",
                        {"to": to, "subject": "Review", "body": "unchanged text"},
                    )
                ]
            }
        ],
    )


def test_missing_version_specific_preparer_refuses_startup(tmp_path: Path) -> None:
    bundle = contract_review.bundle(tmp_path)
    with pytest.raises(ValueError, match="preparation adapters"):
        load_app_rules(replace(bundle, preparers={}))
    with pytest.raises(ValueError, match="preparation adapters"):
        load_app_rules(
            replace(
                bundle, preparers={("email.send", "1.0.0"): next(iter(bundle.preparers.values()))}
            )
        )


@pytest.mark.parametrize(
    "extra",
    [{"delivery": {}}, {"delivery": {"$ref": "x.output"}}, {"to": {"delivery": {"mailboxes": []}}}],
)
def test_forged_delivery_is_rejected(tmp_path: Path, extra: dict[str, Any]) -> None:
    args = {"to": "legal@acme.example", "subject": "s", "body": "b", **extra}
    plan = {"steps": [step("mail", "email.send", args)]}
    w = World(tmp_path, [plan, plan, plan])
    run_id = w.run(document=None)
    assert w.status(run_id) == "failed"
    assert w.email.server is not None and w.email.server.records() == []


def test_group_delivers_only_terminal_mailboxes(tmp_path: Path) -> None:
    w = world(tmp_path, "all-staff@acme.example")
    run_id = w.run(document=None)
    assert w.status(run_id) == "completed"
    assert w.email.server is not None
    (receipt,) = w.email.server.records()
    assert receipt.accepted_recipients == ("legal@acme.example", "ops@acme.example")
    assert receipt.header_to == "all-staff@acme.example"


@pytest.mark.parametrize(
    "members",
    [
        ("legal@acme.example",),
        ("legal@acme.example", "ops@acme.example"),
        ("legal@acme.example", "vendor@outside.example"),
    ],
)
def test_changes_require_fresh_approval_and_keep_old_snapshot(
    tmp_path: Path, members: tuple[str, ...]
) -> None:
    w = world(tmp_path)
    assert w.email.server is not None
    directory = w.email.server.directory
    directory.put(
        "mixed@acme.example",
        DirectoryEntry(kind="group", members=("legal@acme.example", "vendor@outside.example")),
    )
    run_id = w.run(document=None)
    (old,) = w.service.inbox(BOB)
    directory.put("mixed@acme.example", DirectoryEntry(kind="group", members=members))
    w.approve(BOB)
    assert w.status(run_id) == "paused_for_approval"
    assert w.email.server.records() == []
    (fresh,) = w.service.inbox(BOB)
    assert fresh.action_hash != old.action_hash
    assert fresh.resolved_args["delivery"]["reapproval_required"] is True
    assert fresh.resolved_args["delivery"]["mailboxes"] == list(members)
    stored_old = w.service._approval_store.get(old.approval_id)
    assert stored_old.resolved_args == old.resolved_args
    assert stored_old.preparation == old.preparation
    # A second directory revision requires a third distinct permission, even with the same set.
    directory.put("mixed@acme.example", DirectoryEntry(kind="group", members=members))
    w.approve(BOB)
    (third,) = w.service.inbox(BOB)
    assert third.action_hash not in {old.action_hash, fresh.action_hash}
    assert w.email.server.records() == []
    w.approve(BOB)
    assert w.status(run_id) == "completed"
    assert len(w.email.server.records()) == 1


@pytest.mark.parametrize(
    "kind,members,status",
    [
        ("unreadable_group", (), "failed"),
        ("group", ("leak@evil.example",), "denied"),
    ],
)
def test_refresh_unreadable_or_blocked_cannot_send(
    tmp_path: Path, kind: Any, members: tuple[str, ...], status: str
) -> None:
    w = world(tmp_path)
    assert w.email.server is not None
    w.email.server.directory.put(
        "mixed@acme.example", DirectoryEntry(kind="group", members=("vendor@outside.example",))
    )
    run_id = w.run(document=None)
    w.email.server.directory.put("mixed@acme.example", DirectoryEntry(kind=kind, members=members))
    w.approve(BOB)
    assert w.status(run_id) == status
    assert w.email.server.records() == []


def test_lookup_time_is_outside_action_hash(tmp_path: Path) -> None:
    bundle = contract_review.bundle(tmp_path)
    prepare = bundle.preparers[("email.send", "2.0.0")]
    args = {"to": "legal@acme.example", "subject": "s", "body": "b"}
    first, second = prepare(args, reapproval=False), prepare(args, reapproval=False)
    assert first.metadata != second.metadata

    def digest(value: Any) -> str:
        return action_hash(
            tool="email.send", version="2.0.0", resolved_args=value.args, run_id="r", step_id="s"
        )

    assert digest(first) == digest(second)


@pytest.mark.parametrize("op", ["any_domain_in", "any_domain_not_in"])
@pytest.mark.parametrize(
    "value",
    [None, [], "a@acme.example", ["a@acme.example", "bad"], ["bad", "a@acme.example"], [None]],
)
def test_list_domains_never_short_circuit_malformed_members(op: str, value: Any) -> None:
    with pytest.raises(PolicyEvaluationError):
        apply_operator(op, value, ["acme.example"])


def test_crash_after_step_decision_before_run_mirror_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_path, "vendor@outside.example")
    original = w.service._runs.update

    def crash(run_id: str, **updates: Any) -> Any:
        if "decisions" in updates:
            raise RuntimeError("crash after atomic step write")
        return original(run_id, **updates)

    monkeypatch.setattr(w.service._runs, "update", crash)
    run_id = w.run(document=None)
    record = w.service._steps.get(run_id, "mail")
    assert record.status is StepStatus.POLICY_CHECKED
    assert record.policy_decision is not None
    monkeypatch.setattr(w.service._runs, "update", original)
    w.service.resume_run(ALICE, run_id)
    assert w.status(run_id) == "paused_for_approval"
    w.approve(BOB)
    assert w.status(run_id) == "completed"


class SimulatedCrash(BaseException):
    pass


def test_accepted_operation_recovers_same_key_despite_directory_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_path, "vendor@outside.example")
    assert w.email.server is not None
    run_id = w.run(document=None)
    old_hash = w.service._steps.get(run_id, "mail").action_hash
    submit = w.email.server.submit

    def crash(*args: Any, **kwargs: Any) -> Any:
        submit(*args, **kwargs)
        raise SimulatedCrash()

    monkeypatch.setattr(w.email.server, "submit", crash)
    with pytest.raises(SimulatedCrash):
        w.approve(BOB)
    w.email.server.directory.put("legal@acme.example", DirectoryEntry(kind="mailbox"))
    monkeypatch.setattr(w.email.server, "submit", submit)
    resolver = w.service._orch._executor.verification
    assert resolver is not None
    job = resolver.jobs.for_run(ALICE.tenant_id, run_id)[0]
    monkeypatch.setattr(resolver, "clock", lambda: job.created_at + timedelta(seconds=31))
    w.service.resume_run(ALICE, run_id)
    assert w.status(run_id) == "completed"
    assert w.service._steps.get(run_id, "mail").action_hash == old_hash
    assert len(w.email.server.records()) == 1
    assert w.service.inbox(BOB) == []


def test_automatic_send_racing_directory_change_requires_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_path, "all-staff@acme.example")
    assert w.email.server is not None
    server = w.email.server
    submit = server.submit
    changed = False

    def race(*args: Any, **kwargs: Any) -> Any:
        nonlocal changed
        if not changed:
            changed = True
            kwargs["before_check"] = lambda: server.directory.put(
                "all-staff@acme.example",
                DirectoryEntry(kind="group", members=("legal@acme.example",)),
            )
        return submit(*args, **kwargs)

    monkeypatch.setattr(server, "submit", race)
    run_id = w.run(document=None)
    assert w.status(run_id) == "paused_for_approval"
    assert server.records() == []
    w.approve(BOB)
    assert w.status(run_id) == "completed"
    assert server.records()[0].accepted_recipients == ("legal@acme.example",)


@pytest.mark.parametrize(
    "status",
    [StepStatus.READY, StepStatus.AWAITING_APPROVAL, StepStatus.EXECUTING, StepStatus.VERIFYING],
)
def test_retired_email_version_never_executes(tmp_path: Path, status: StepStatus) -> None:
    w = world(tmp_path, "vendor@outside.example")
    run_id = w.run(document=None)
    store = w.service._steps
    original = store.get(run_id, "mail")
    # Reproduce a persisted v1 row at an upgrade boundary, without rewriting its hash.
    store._steps[(run_id, "mail")] = original.model_copy(
        update={"tool_version": "1.0.0", "status": status}
    )
    w.service.resume_run(ALICE, run_id)
    assert w.status(run_id) == "failed"
    expected = (
        "legacy_send_reconciliation_required"
        if status in {StepStatus.EXECUTING, StepStatus.VERIFYING}
        else "unsupported_email_version"
    )
    assert store.get(run_id, "mail").error == expected
    assert w.email.server is not None and w.email.server.records() == []


def test_crash_after_rejected_attempt_recovers_refresh_without_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_path, "vendor@outside.example")
    assert w.email.server is not None
    run_id = w.run(document=None)
    w.email.server.directory.put("legal@acme.example", DirectoryEntry(kind="mailbox"))
    original = w.service._steps.transition

    def crash(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("to") is StepStatus.READY:
            raise SimulatedCrash()
        return original(*args, **kwargs)

    monkeypatch.setattr(w.service._steps, "transition", crash)
    with pytest.raises(SimulatedCrash):
        w.approve(BOB)
    assert w.service._steps.get(run_id, "mail").status is StepStatus.EXECUTING
    monkeypatch.setattr(w.service._steps, "transition", original)
    monkeypatch.setattr(w.service._orch._executor, "_lease", timedelta(0))
    w.service.resume_run(ALICE, run_id)
    assert w.status(run_id) == "paused_for_approval"
    assert w.email.server.records() == []
    assert len(w.service._orch._executions.attempts(run_id, "mail")) == 1
    w.approve(BOB)
    assert w.status(run_id) == "completed"


def test_reference_is_resolved_before_preparing(tmp_path: Path) -> None:
    w = World(
        tmp_path,
        [
            {
                "steps": [
                    step("get", "clm.get_contract", {"contract_id": "C-1001"}),
                    step(
                        "mail",
                        "email.send",
                        {
                            "to": {"$ref": "get.output.counterparty_contact"},
                            "subject": "Review",
                            "body": "unchanged text",
                        },
                        ("get",),
                    ),
                ]
            }
        ],
    )
    run_id = w.run(document=None)
    assert w.status(run_id) == "paused_for_approval"
    (item,) = w.service.inbox(BOB)
    assert isinstance(item.resolved_args["to"], str)
    assert item.resolved_args["delivery"]["mailboxes"] == [item.resolved_args["to"]]


def test_obsolete_pending_approval_is_closed_during_refresh_recovery(tmp_path: Path) -> None:
    w = world(tmp_path, "vendor@outside.example")
    assert w.email.server is not None
    run_id = w.run(document=None)
    old = w.service._approval_store.for_step(run_id, "mail")[0]
    w.email.server.directory.put("legal@acme.example", DirectoryEntry(kind="mailbox"))
    w.approve(BOB)
    (fresh,) = w.service.inbox(BOB)
    # Model a stale worker inserting an old request just before the fresh request was saved.
    records = w.service._approval_store._records
    del records[fresh.approval_id]
    obsolete = old.model_copy(update={"approval_id": str(uuid4())})
    w.service._approval_store.add(obsolete)
    w.service.resume_run(ALICE, run_id)
    assert w.service._approval_store.get(obsolete.approval_id).decision == "rejected"
    (recovered,) = w.service.inbox(BOB)
    assert recovered.action_hash == fresh.action_hash
    assert w.email.server.records() == []
    w.approve(BOB)
    assert w.status(run_id) == "completed"
