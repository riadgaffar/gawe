"""M22 S2: conditions a write relies on that its target can't enforce, read just before sending.

The IT ticket lives in the ticket system, not the IAM system, so a grant's write can't check it.
Before M22 a ticket closed while its grant waited for approval still got the grant.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.helpers import signer
from tests.unit.test_trusted_submitter import (
    AGENT,
    LEE,
    SAM,
    SECURITY,
    findings,
    grant_plan,
)

from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, load_app_rules, memory_stores
from gawe.apps import it_access as ia
from gawe.connectors.base import Ledger
from gawe.connectors.mock_tickets import MockTickets
from gawe.core.guarantees import DispatchCondition
from gawe.llm.fake import FakeProvider

GRANT = ("iam.grant_access", "2.0.0")


class Desk:
    """IT access where Lee asks for Sam's access, so the grant waits for a security approver."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tickets = MockTickets.in_state_dir(path)
        self.bundle = ia.bundle(path, tickets=self.tickets)
        self.iam = self.bundle.revokers[GRANT]
        self.stores = memory_stores()
        self.service = assemble(
            self.bundle,
            stores=self.stores,
            signer=signer(),
            provider=FakeProvider([findings(SAM), grant_plan(SAM)]),
            background=InlineBackground(),
        )
        self.tickets.create("t1", "T-1", submitted_by=LEE, channel="portal", body="ticket")
        started = self.service.start_run(
            AGENT, objective="Process the request", profile="it_access", source_id="T-1"
        )
        self.run_id = started.run_id
        assert self.view().status == "paused_for_approval"

    def view(self) -> Any:
        return self.service.get_run(AGENT, self.run_id)

    def approve(self) -> Any:
        (item,) = self.service.inbox(SECURITY)
        self.service.decide(
            SECURITY, item.approval_id, decision="approved", action_hash=item.action_hash
        )
        return self.view()

    def grants(self) -> list[dict[str, Any]]:
        return Ledger(self.path / "ledger.jsonl").records("iam.grant")

    def checks(self) -> list[Any]:
        return [e for e in self.stores.audit.events(self.run_id) if e.type == "condition.checked"]


def test_a_ticket_closed_while_the_grant_waits_is_refused_at_dispatch(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.tickets.set_status("t1", "T-1", "closed")
    view = d.approve()
    assert view.status == "failed"
    assert view.steps[-1].status == "failed" and view.steps[-1].error == "condition_changed"
    assert d.grants() == [] and d.iam.operations.records() == []
    assert len(d.checks()) == 1


def test_a_ticket_reopened_before_the_approval_is_granted(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.tickets.set_status("t1", "T-1", "closed")
    d.tickets.set_status("t1", "T-1", "open")
    assert d.approve().status == "completed" and len(d.grants()) == 1


def test_a_ticket_closed_between_the_check_and_the_write_is_the_named_race(
    tmp_path: Path,
) -> None:
    # The window D2 accepts: the check held, then the ticket closed before the IAM wrote.
    d = Desk(tmp_path)
    d.iam.before_guarded_write = lambda: d.tickets.set_status("t1", "T-1", "closed")
    assert d.approve().status == "completed" and len(d.grants()) == 1
    assert len(d.checks()) == 1  # it held when read; the audit says so, never what was read


def test_an_unreadable_ticket_at_dispatch_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = Desk(tmp_path)

    def down(*args: Any) -> Any:
        raise OSError("ticket system unavailable")

    monkeypatch.setattr(d.tickets, "get", down)
    view = d.approve()
    assert view.steps[-1].error == "condition_unreadable" and d.grants() == []


def test_a_retry_checks_again(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.iam.faults.fail_next(1)  # the first attempt is rejected before dispatch; the retry runs
    assert d.approve().status == "completed" and len(d.grants()) == 1
    assert len(d.checks()) == 2


def test_the_ticket_closing_before_a_retry_stops_it(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.iam.faults.fail_next(1)
    original = d.iam.faults.check

    def close_after_first_failure() -> None:
        try:
            original()
        except Exception:
            d.tickets.set_status("t1", "T-1", "closed")  # while the executor waits to retry
            raise

    d.iam.faults.check = close_after_first_failure  # type: ignore[method-assign]
    view = d.approve()
    assert view.steps[-1].error == "condition_changed" and d.grants() == []
    assert len(d.checks()) == 2


def test_the_audit_names_the_condition_and_the_result_never_the_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = Desk(tmp_path)
    audit = d.service._audit
    original, seen = audit.record, []

    def spy(run_id: str, type: str, actor: str, payload: Any = None, **kwargs: Any) -> Any:
        if type == "condition.checked":
            seen.append(payload)
        return original(run_id, type, actor, payload, **kwargs)

    monkeypatch.setattr(audit, "record", spy)
    d.tickets.set_status("t1", "T-1", "closed")
    d.approve()
    (payload,) = seen
    assert payload["conditions"] == ["ticket_open"] and payload["result"] == "changed:ticket_open"
    assert set(payload) == {"step_id", "action_hash", "conditions", "result"}
    assert "T-1" not in str(payload) and SAM not in str(payload) and LEE not in str(payload)


def test_the_run_view_names_what_was_only_checked(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    (grant,) = d.view().steps
    assert grant.checked_not_enforced == ("ticket_open",)


def test_contract_review_writes_declare_no_condition_and_are_unchanged(tmp_path: Path) -> None:
    from tests.unit.test_write_readback import ALICE, Env

    e = Env(tmp_path, "crm")
    e.advance()
    assert not e.bundle.dispatch_conditions
    run = e.service.get_run(ALICE, e.run_id)
    assert all(s.checked_not_enforced == () for s in run.steps)
    assert not [x for x in e.stores.audit.events(e.run_id) if x.type == "condition.checked"]


# --- startup -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("conditions", "message"),
    [
        ({("iam.lookup_user", "1.0.0"): "ok"}, "not a current write"),
        ({("iam.grant_access", "1.0.0"): "ok"}, "not a current write"),
        ({("iam.grant_access", "9.9.9"): "ok"}, "not a current write"),
        ({GRANT: "Not A Name"}, "distinct short names"),
        ({GRANT: ("dup", "dup")}, "distinct short names"),
    ],
)
def test_conditions_name_current_writes_with_distinct_short_names(
    tmp_path: Path, conditions: dict[Any, Any], message: str
) -> None:
    def named(names: Any) -> tuple[DispatchCondition, ...]:
        names = (names,) if isinstance(names, str) else names
        return tuple(DispatchCondition(n, lambda run, step: True) for n in names)

    bundle = ia.bundle(tmp_path)
    declared = {k: named(v) for k, v in conditions.items()}
    with pytest.raises(ValueError, match=message):
        load_app_rules(replace(bundle, dispatch_conditions=declared))


def test_the_it_app_declares_ticket_open_for_grants(tmp_path: Path) -> None:
    bundle = ia.bundle(tmp_path)
    assert [c.name for c in bundle.dispatch_conditions[GRANT]] == ["ticket_open"]
    assert load_app_rules(bundle)


def test_a_crash_after_the_check_and_before_the_call_never_sends_on_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.flow import Crash

    d = Desk(tmp_path)
    executor = d.service._orch._executor
    checked = executor._check_conditions

    def check_then_crash(step: Any) -> Any:
        checked(step)
        raise Crash()  # the process dies with the check done and nothing sent

    monkeypatch.setattr(executor, "_check_conditions", check_then_crash)
    with pytest.raises(Crash):
        d.approve()
    after = assemble(  # a new process over the same state
        d.bundle,
        stores=d.stores,
        signer=signer(),
        provider=FakeProvider([]),
        background=InlineBackground(),
    )
    after.recover_interrupted()
    after._orch.advance(d.run_id)
    assert d.grants() == [] and d.iam.operations.records() == []
