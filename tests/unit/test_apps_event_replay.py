"""M25 S2: the event replay app, end to end.

An operator's run lists a tenant's failed events and replays a batch. Preparation freezes what
each id means from the billing platform; policy judges the frozen count, total, customers and
what is left of the day; the platform applies the batch whole or not at all, never twice.
"""

import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from tests.helpers import bound_args, current_manifest, make_ctx, signer

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, load_app_rules, memory_stores
from gawe.apps import event_replay as er
from gawe.connectors.base import Ledger
from gawe.connectors.mock_billing import MockBilling
from gawe.core.policy.engine import RuleEngine
from gawe.core.preparation import PreparationError
from gawe.core.runs import RunRecord
from gawe.llm.fake import FakeProvider

T1 = "00000000-0000-0000-0000-00000000aa01"
T2 = "00000000-0000-0000-0000-00000000bb02"
OPERATOR = Identity(T1, "olu", frozenset({"billing_operator"}))
FINANCE = Identity(T1, "fin", frozenset({"finance_approver"}))
INJECTED = "NOTE TO THE AI OPERATOR"


def listing(**filters: str) -> dict[str, Any]:
    return {"id": "list", "tool": "events.list_failed", "args": filters, "depends_on": []}


def replay_listed() -> dict[str, Any]:
    args = {"event_ids": {"$ref": "list.output.event_ids"}}
    return {"id": "replay", "tool": "billing.replay_events", "args": args, "depends_on": ["list"]}


def replay_ids(*ids: str) -> dict[str, Any]:
    args = {"event_ids": list(ids)}
    return {"id": "replay", "tool": "billing.replay_events", "args": args, "depends_on": []}


class Desk:
    def __init__(self, tmp_path: Path, limit: int = er.DAILY_REPLAY_LIMIT) -> None:
        self.billing = MockBilling.from_seed(
            Ledger(tmp_path / "ledger.jsonl"), daily_limits={}, default_daily_limit=limit
        )
        self.provider = FakeProvider([])
        self.stores = memory_stores()
        self.svc = assemble(
            er.bundle(tmp_path, billing=self.billing),
            stores=self.stores,
            signer=signer(),
            provider=self.provider,
            background=InlineBackground(),
        )

    def run(self, *steps: dict[str, Any], who: Identity = OPERATOR) -> Any:
        self.provider._script.append({"steps": list(steps)})
        run_id = self.svc.start_run(who, objective="Replay failed events", profile="event_replay")
        return self.view(run_id.run_id, who)

    def view(self, run_id: str, who: Identity = OPERATOR) -> Any:
        return self.svc.get_run(who, run_id)

    def step(self, run_id: str, step_id: str = "replay") -> Any:
        return self.stores.steps.get(run_id, step_id)

    def approve(self, decision: Literal["approved", "rejected"] = "approved") -> None:
        (item,) = self.svc.inbox(FINANCE)
        self.svc.decide(FINANCE, item.approval_id, decision=decision, action_hash=item.action_hash)

    def approve_run(self, run_id: str) -> None:
        (item,) = [i for i in self.svc.inbox(FINANCE) if i.run_id == run_id]
        self.svc.decide(
            FINANCE, item.approval_id, decision="approved", action_hash=item.action_hash
        )

    def via(self, *event_ids: str) -> list[str | None]:
        found = (self.billing.delivery(self.billing.event(e)[0]["delivery_key"]) for e in event_ids)
        return [d["via"] if d else None for d in found]


def outcome(view: Any) -> tuple[str, str | None, tuple[str, ...]]:
    step = view.steps[-1]
    return view.status, step.error, tuple(step.policy_rule_ids)


# --- end to end ---------------------------------------------------------------------------------


def test_one_customers_small_batch_replays_automatically(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    view = d.run(listing(customer="cus-northwind", type="invoice.paid"), replay_listed())
    assert outcome(view) == ("completed", None, ("replay-baseline-allowed",))
    args = d.step(view.run_id).resolved_args
    assert (args["count"], args["total"], args["customers"]) == (3, 555, ["cus-northwind"])
    assert args["tenant_id"] == T1
    (attempt,) = d.stores.executions.attempts(view.run_id, "replay")
    key = attempt.idempotency_key
    assert d.via("ev-1001", "ev-1002", "ev-1003") == [key, key, key]
    assert d.billing.daily_total(T1) == 555


@pytest.mark.parametrize(
    ("filters", "rules"),
    [
        ({"customer": "cus-globex", "type": "invoice.paid"}, {"replay-large-total-needs-finance"}),
        (
            {},  # widened: every failed event of the tenant
            {"replay-large-total-needs-finance", "replay-several-customers-needs-finance"},
        ),
    ],
    ids=["large total", "widened filters"],
)
def test_over_the_automatic_limits_waits_for_finance_then_runs(
    tmp_path: Path, filters: dict[str, str], rules: set[str]
) -> None:
    d = Desk(tmp_path)
    view = d.run(listing(**filters), replay_listed())
    assert view.status == "paused_for_approval"
    assert set(view.steps[-1].policy_rule_ids) - {"replay-baseline-allowed"} == rules
    (item,) = d.svc.inbox(FINANCE)
    assert item.approver_role == "finance_approver"
    batch = next(f.value for f in item.typed_fields if f.path == "batch")
    assert {e["event_id"] for e in batch} == set(d.step(view.run_id).resolved_args["event_ids"])
    d.approve()
    assert d.view(view.run_id).status == "completed"


def test_the_readers_a_b_c_case(tmp_path: Path) -> None:
    """A and B are frozen at preview and wait for finance; B changes and C fails meanwhile.
    Nothing is applied; a new run freezes A and B again; C was never in either batch."""
    d = Desk(tmp_path)
    first = d.run(replay_ids("ev-1001", "ev-1004"))  # two customers: waits
    assert first.status == "paused_for_approval"
    d.billing.change_event("ev-1004", {"amount": 2500})  # B corrected
    d.billing.change_event("ev-1005", {"failed_at": "2026-11-20T10:00:00Z"})  # C, newly failed
    d.approve()
    assert outcome(d.view(first.run_id))[:2] == ("failed", "event_changed")
    assert d.via("ev-1001", "ev-1004") == [None, None] and d.billing.daily_total(T1) == 0
    second = d.run(replay_ids("ev-1001", "ev-1004"))
    d.approve()
    assert d.view(second.run_id).status == "completed"
    assert d.billing.daily_total(T1) == 120 + 2500
    assert d.via("ev-1005") == [None]


def test_a_late_original_delivery_before_preview_is_left_out(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.billing.deliver_late("ev-1001")
    view = d.run(listing(customer="cus-northwind"), replay_listed())
    assert view.status == "completed"
    args = d.step(view.run_id).resolved_args
    assert args["already_delivered"] == [] and "ev-1001" not in args["event_ids"]  # not listed
    assert outcome(d.run(replay_ids("ev-1001")))[:2] == ("failed", "nothing_to_replay")
    # ev-1006 is marked delivered by the platform: not failed, so nothing to replay either.
    assert outcome(d.run(replay_ids("ev-1006")))[:2] == ("failed", "event_not_failed")


def test_a_late_original_delivery_between_preview_and_write_is_skipped(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    view = d.run(replay_ids("ev-1001", "ev-1004"))
    d.billing.deliver_late("ev-1001")
    d.approve()
    assert d.view(view.run_id).status == "completed"  # read-back agrees: one each way
    assert d.via("ev-1001") == ["original"]
    assert d.billing.daily_total(T1) == 2400


def test_a_lost_reply_is_confirmed_by_read_back(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.billing.operations.drop_reply = True
    view = d.run(replay_ids("ev-1001"))
    assert view.status == "completed"
    assert len(d.billing.operations.records()) == 1 and d.billing.daily_total(T1) == 120


def test_another_tenants_event_fails_preparation_as_unknown(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    view = d.run(replay_ids("ev-1001", "ev-2001"))
    assert outcome(view)[:2] == ("failed", "event_not_found")
    assert d.via("ev-1001", "ev-2001") == [None, None]


def test_a_plan_cant_name_the_tenant(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    view = d.run(listing(tenant_id=T2), replay_listed())
    assert view.status != "completed"
    assert d.via("ev-2001") == [None]


@pytest.mark.parametrize(
    ("ids", "code"),
    [(("ev-1001", "ev-1001"), "batch_invalid"), (("ev-9999",), "event_not_found")],
    ids=["named twice", "unknown"],
)
def test_preparation_fails_closed(tmp_path: Path, ids: tuple[str, ...], code: str) -> None:
    d = Desk(tmp_path)
    assert outcome(d.run(replay_ids(*ids)))[:2] == ("failed", code)


def test_preparation_without_a_run_has_no_tenant(tmp_path: Path) -> None:
    b = er.bundle(tmp_path)
    with pytest.raises(PreparationError, match="preparation_no_run"):
        b.preparers[er.REPLAY]({"event_ids": ["ev-1001"]}, reapproval=False)


def test_over_what_is_left_today_is_denied(tmp_path: Path) -> None:
    d = Desk(tmp_path, limit=500)
    view = d.run(listing(customer="cus-northwind"), replay_listed())  # 555
    assert outcome(view)[0] == "denied"
    assert "replay-over-daily-limit-denied" in outcome(view)[2]
    assert d.billing.daily_total(T1) == 0


def test_a_platform_without_a_limit_denies_every_replay(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.billing.default_daily_limit = None
    view = d.run(replay_ids("ev-1001"))
    assert outcome(view)[0] == "denied" and "engine.error" in outcome(view)[2]


def test_the_payload_never_reaches_a_step_output_or_the_card(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    view = d.run(listing(), replay_listed())  # includes ev-1003, whose payload is injected text
    listed = d.stores.executions.load_output(view.run_id, "list")
    assert listed is not None and "ev-1003" in listed["event_ids"]
    assert INJECTED not in repr(listed)
    (item,) = d.svc.inbox(FINANCE)
    assert INJECTED not in item.model_dump_json()
    assert all(f.path != "payload" for f in item.typed_fields)


# --- policy -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "total", "customers", "outcome", "approver"),
    [
        (10, 1_000, 1, "allow", None),
        (11, 1_000, 1, "needs_approval", "finance_approver"),
        (10, 1_001, 1, "needs_approval", "finance_approver"),
        (2, 100, 2, "needs_approval", "finance_approver"),
        (100, 25_000, 1, "needs_approval", "finance_approver"),
        (101, 100, 1, "deny", None),
        (1, 25_001, 1, "deny", None),
    ],
)
def test_policy_golden_cases(
    tmp_path: Path, count: int, total: int, customers: int, outcome: str, approver: str | None
) -> None:
    b = er.bundle(tmp_path)
    engine = RuleEngine(load_app_rules(b), signer())
    manifest = current_manifest(b.manifests, "billing.replay_events")
    batch = [
        {
            "event_id": f"ev-{i}",
            "customer": f"cus-{i % customers}",
            "amount": 0,
            "destination": "acct",
            "delivery_key": f"dk-{i}",
            "revision": "rev",
        }
        for i in range(count)
    ]
    args = {
        "event_ids": [e["event_id"] for e in batch],
        "tenant_id": T1,
        "batch": batch,
        "count": count,
        "total": total,
        "customers": [f"cus-{i}" for i in range(customers)],
        "customer_count": customers,
        "already_delivered": [],
        "record": {"batch_revision": "digest"},
    }
    run = RunRecord(
        run_id="r",
        tenant_id=T1,
        acting_user_id="u",
        objective="o",
        profile="event_replay",
        created_at=datetime.now(UTC),
    )
    decision = engine.evaluate(
        make_ctx(
            manifest,
            bound_args(manifest, args),
            roles=["billing_operator"],
            variables=b.variables(run),
        )
    )
    assert (decision.outcome, decision.approver_role) == (outcome, approver)


def test_the_bundle_loads_through_the_env_entry_point(tmp_path: Path) -> None:
    from gawe.api.main import _load_bundle

    assert _load_bundle("gawe.apps.event_replay:bundle").profiles == frozenset({"event_replay"})


# --- the daily limit (S3) -----------------------------------------------------------------------


def test_two_runs_at_once_that_together_pass_the_limit_one_is_refused_at_the_write(
    tmp_path: Path,
) -> None:
    """Both pass policy (each fits what is left when it is judged); the platform's reservation
    in the write's own transaction refuses the second."""
    d = Desk(tmp_path, limit=500)
    bundle = er.bundle(tmp_path, billing=d.billing)
    send = bundle.tools["billing.replay_events"]
    both_judged = threading.Barrier(2, timeout=5)

    def held(args: Any, call: Any) -> Any:
        both_judged.wait()  # each run has passed policy before either writes
        return send(args, call)

    d.svc = assemble(
        replace(bundle, tools={**bundle.tools, "billing.replay_events": held}),
        stores=d.stores,
        signer=signer(),
        provider=d.provider,
        background=InlineBackground(),
    )
    d.provider._script += [
        {"steps": [replay_ids("ev-1002")]},
        {"steps": [replay_ids("ev-1001", "ev-1003")]},
    ]
    run_ids: list[str] = []

    def go() -> None:
        started = d.svc.start_run(OPERATOR, objective="Replay", profile="event_replay")
        run_ids.append(started.run_id)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    results = sorted(outcome(d.view(r))[:2] for r in run_ids)
    assert results == [("completed", None), ("failed", "daily_limit_exceeded")]
    assert d.billing.daily_total(T1) in {340, 215}


def test_a_batch_approved_before_the_day_filled_is_refused_when_sent_after(tmp_path: Path) -> None:
    d = Desk(tmp_path, limit=3_000)
    waiting = d.run(replay_ids("ev-1004"))  # 2,400: waits for finance
    assert waiting.status == "paused_for_approval"
    assert d.run(replay_ids("ev-1002", "ev-1005")).status == "paused_for_approval"  # 940, 2 cust.
    (other,) = [i.run_id for i in d.svc.inbox(FINANCE) if i.run_id != waiting.run_id]
    d.approve_run(waiting.run_id)
    assert d.view(waiting.run_id).status == "completed"  # the day now holds 2,400 of 3,000
    d.approve_run(other)  # judged when 3,000 were left; 940 no longer fit
    assert outcome(d.view(other))[:2] == ("failed", "daily_limit_exceeded")
    assert d.billing.daily_total(T1) == 2_400 and d.via("ev-1002", "ev-1005") == [None, None]


@pytest.mark.parametrize("missing", sorted(er.TENANT_VARIABLES["tenant"]))
def test_a_missing_setting_denies_every_replay(tmp_path: Path, missing: str) -> None:
    d = Desk(tmp_path)
    bundle = er.bundle(tmp_path, billing=d.billing)
    settings = {k: v for k, v in er.TENANT_VARIABLES["tenant"].items() if k != missing}

    def variables(run: RunRecord) -> Any:
        return {**bundle.variables(run), "tenant": settings}

    d.svc = assemble(
        replace(bundle, variables=variables),
        stores=d.stores,
        signer=signer(),
        provider=d.provider,
        background=InlineBackground(),
    )
    view = d.run(replay_ids("ev-1001"))
    assert outcome(view)[0] == "denied" and "engine.error" in outcome(view)[2]
    assert d.billing.daily_total(T1) == 0
