"""Next-day review: audit-backed re-checks, re-evaluation labels and the keyed sample."""

import threading
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from secrets import token_bytes, token_hex
from typing import Any

import pytest
from tests.flow import Clock
from tests.helpers import KEY, seal_v1, signer

from gawe.anchoring.file_sink import FileAnchorSink
from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.errors import ConflictError, ForbiddenError, NotFoundError
from gawe.api.service import ApiService
from gawe.api.wiring import assemble, memory_stores
from gawe.apps import contract_review
from gawe.apps.contract_review.tools import implementations
from gawe.connectors.base import Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_email import MockEmail
from gawe.connectors.mock_esign import MockESign
from gawe.core.audit import verify_chain
from gawe.core.maintenance import MaintenanceJob
from gawe.core.models import PolicyDecision
from gawe.core.review import KIND, ReviewKey, review_job
from gawe.core.statemachine import RunStatus
from gawe.llm.fake import FakeProvider
from gawe.maintenance.runner import Heartbeat, MaintenanceRunner

ALICE = Identity("t1", "alice", frozenset({"case_manager"}))
BOB = Identity("t1", "bob", frozenset({"legal_reviewer"}))
REVIEWING_REQUESTER = Identity("t1", "alice", frozenset({"case_manager", "legal_reviewer"}))
CAROL = Identity("t2", "carol", frozenset({"case_manager"}))
DAN = Identity("t2", "dan", frozenset({"legal_reviewer"}))
LOW = {"contract_id": "C-1001", "risk_level": "low", "summary": "Mutual, capped liability."}
DAY = date(2026, 1, 1)  # tests.flow.T0 is 2026-01-01 12:00 UTC
REVIEW_TIME = datetime.fromisoformat("2026-01-02T01:00:00+00:00")
ALWAYS = 1.0  # sample rate that draws every candidate


def rate_step(args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "rate", "tool": "crm.write_assessment", "args": args or LOW, "depends_on": []}


class Desk:
    """Contract review over memory stores, with a review key and an anchor sink."""

    def __init__(
        self,
        path: Path,
        *,
        key: ReviewKey | None = None,
        rate: float | None = None,
        policy: Path | None = None,
    ) -> None:
        self.path, self.clock = path, Clock()
        self.stores = memory_stores()
        ledger = Ledger(path / "ledger.jsonl")
        self.clm = MockCLM.from_seed(contract_review.HERE / "samples/clm_seed.json", ledger)
        self.mail = MockEmail(ledger)
        self.esign = MockESign(ledger, self.clm)
        self.sink = FileAnchorSink(path / "anchors.jsonl")
        self.key, self.rate, self.policy = key, rate, policy
        self.provider = FakeProvider([])
        self.service = self.restart()
        self.count = 0

    def bundle(self) -> Any:
        b = contract_review.bundle(self.path, email=self.mail, clm=self.clm)
        return replace(
            b,
            tools=implementations(self.clm, self.mail, self.esign),
            readbacks=contract_review.readbacks(self.clm, self.esign, self.mail),
            policy_rules=self.policy or b.policy_rules,
        )

    def restart(self, **changes: Any) -> ApiService:
        for name, value in changes.items():
            setattr(self, name, value)
        service = assemble(
            self.bundle(),
            stores=self.stores,
            signer=signer(),
            provider=self.provider,
            background=InlineBackground(),
            clock=self.clock,
            anchor_sinks=[self.sink],
            review_key=self.key,
        )
        if self.rate is not None:
            handler = next(h for h in self.runner_of(service)._handlers.values() if h.kind == KIND)
            handler._rate = self.rate  # type: ignore[attr-defined]
        self.service = service
        return service

    @staticmethod
    def runner_of(service: ApiService) -> MaintenanceRunner:
        assert service.maintenance_runner is not None
        return service.maintenance_runner

    @property
    def runner(self) -> MaintenanceRunner:
        return self.runner_of(self.service)

    def run(self, who: Identity = ALICE, step: dict[str, Any] | None = None) -> str:
        self.count += 1
        run_id = f"run-{self.count}"
        self.provider._script.append({"steps": [step or rate_step()]})
        orch = self.service._orch
        orch.start_run(
            tenant_id=who.tenant_id,
            acting_user_id=who.user_id,
            roles=tuple(who.roles),
            objective="Rate the contract",
            profile="contract_review",
            run_id=run_id,
        )
        assert orch.advance(run_id).status is RunStatus.COMPLETED
        return run_id

    def review(self) -> Heartbeat:
        self.clock.now = REVIEW_TIME
        (beat,) = [b for b in self.runner.tick() if b.kind == KIND]
        return beat

    def items(self, tenant: str = "t1") -> list[Any]:
        return self.stores.reviews.list(tenant)

    def reasons(self, tenant: str = "t1") -> list[tuple[str, str | None, str]]:
        return sorted(
            ((i.run_id, i.step_id, i.reason) for i in self.items(tenant)),
            key=lambda r: (r[0], r[1] or "", r[2]),
        )

    def rewrite_decision(self, run_id: str, **update: Any) -> None:
        steps = self.stores.steps._steps  # type: ignore[attr-defined]
        step = steps[(run_id, "rate")]
        decision = step.policy_decision
        steps[(run_id, "rate")] = step.model_copy(
            update={"policy_decision": decision.model_copy(update=update)}
        )

    def events(self, run_id: str, kind: str) -> list[Any]:
        return [e for e in self.stores.audit.events(run_id) if e.type == kind]


def key() -> ReviewKey:
    return ReviewKey(bytes(range(100, 132)))


# --- scheduling -----------------------------------------------------------------------------------


def test_one_job_per_tenant_and_day_due_at_one_in_the_morning(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.run(ALICE)
    d.run(CAROL)
    d.clock.now = REVIEW_TIME - timedelta(minutes=30)  # 00:30 the next day
    beat = next(b for b in d.runner.tick() if b.kind == KIND)
    # 7 days x 2 tenants; the 6 earlier days are already due (and empty), Jan 1 is not.
    assert beat.scheduled == 14 and beat.checked == 12
    for tenant in ("t1", "t2"):
        job = d.stores.maintenance.get(tenant, review_job(tenant, DAY).job_id)
        assert job is not None and job.due_at == REVIEW_TIME and job.run_id is None
        assert job.payload == {"day": "2026-01-01"}
    assert next(b for b in d.runner.tick() if b.kind == KIND).scheduled == 0  # nothing new
    beat = d.review()
    assert beat.checked == 2 and beat.outcomes["reviewed"] == 2


def test_racing_runners_create_one_job(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.run(ALICE)
    d.clock.now = REVIEW_TIME - timedelta(minutes=30)
    runners = [d.runner_of(d.restart()) for _ in range(4)]
    start = threading.Barrier(len(runners))
    beats: list[Heartbeat] = []

    def go(r: MaintenanceRunner) -> None:
        start.wait()
        beats.append(next(b for b in r.tick() if b.kind == KIND))

    threads = [threading.Thread(target=go, args=(r,)) for r in runners]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(b.scheduled for b in beats) == 7  # each of the last 7 days, once


def test_days_missed_while_no_scheduler_ran_are_still_reviewed(tmp_path: Path) -> None:
    d = Desk(tmp_path, key=key(), rate=ALWAYS)
    first = d.run()  # Jan 1
    d.clock.now = datetime.fromisoformat("2026-01-02T12:00:00+00:00")
    second = d.run()  # Jan 2
    d.clock.now = datetime.fromisoformat("2026-01-05T03:00:00+00:00")  # no tick since Jan 1
    beat = next(b for b in d.runner.tick() if b.kind == KIND)
    assert beat.scheduled == 7 and beat.outcomes["reviewed"] == 7
    assert sorted((i.day.isoformat(), i.run_id) for i in d.items()) == [
        ("2026-01-01", first),
        ("2026-01-02", second),
    ]
    d.clock.advance(days=7)  # a week later the window has moved on
    beat = next(b for b in d.runner.tick() if b.kind == KIND)
    assert beat.scheduled == 7 and len(d.items()) == 2  # nothing reviewed twice


def test_a_review_job_belongs_to_a_tenant_not_a_run() -> None:
    job = review_job("t1", DAY)
    assert (job.run_id, job.plan_version, job.step_id, job.action_hash) == (None,) * 4
    with pytest.raises(ValueError, match="belongs to a tenant"):
        MaintenanceJob(**{**job.model_dump(), "run_id": "run-1"})
    with pytest.raises(ValueError, match="needs its run"):
        MaintenanceJob(**{**job.model_dump(), "kind": "write_readback"})


# --- a clean day --------------------------------------------------------------------------------


def test_a_clean_day_without_a_sample_key_records_nothing(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.run()
    beat = d.review()
    assert beat.outcomes["reviewed"] == 7 and d.items() == []  # the last 7 days, once each
    review = d.stores.maintenance.get("t1", review_job("t1", DAY).job_id)
    assert review is not None and review.continued
    assert review.result == {
        "outcome": "reviewed",
        "code": "daily_review",
        "items": 0,
        "sampled": 0,
        "sample": "off",
    }
    assert next(b for b in d.runner.tick() if b.kind == KIND).checked == 0


# --- tampering ----------------------------------------------------------------------------------


def test_a_tampered_stored_decision_is_an_audit_mismatch(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    run_id = d.run()
    d.rewrite_decision(run_id, rule_ids=("crm-assessment-allowed", "added-later"))
    d.review()
    assert d.reasons() == [(run_id, "rate", "audit_mismatch")]


def test_a_bad_signature_is_flagged(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    run_id = d.run()
    d.rewrite_decision(run_id, signature="0" * 64)  # the signature is not in the audited payload
    d.review()
    assert d.reasons() == [(run_id, "rate", "signature_invalid")]


def test_tampered_stored_arguments_are_an_action_mismatch(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    run_id = d.run()
    steps = d.stores.steps._steps  # type: ignore[attr-defined]
    step = steps[(run_id, "rate")]
    args = {**step.resolved_args, "risk_level": "high"}
    steps[(run_id, "rate")] = step.model_copy(update={"resolved_args": args})
    d.review()
    assert d.reasons() == [(run_id, "rate", "action_mismatch")]


def test_a_tampered_audit_event_breaks_the_chain_and_is_flagged(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    run_id = d.run()
    chain = d.stores.audit._events[run_id]  # type: ignore[attr-defined]
    i = next(n for n, e in enumerate(chain) if e.type == "policy.decided")
    chain[i] = chain[i].model_copy(update={"payload_hash": "f" * 64})
    d.review()
    assert (run_id, None, "audit_mismatch") in d.reasons()


def test_an_unmatched_event_after_a_recipient_refresh_is_superseded(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    run_id, other = d.run(), d.run()
    for rid in (run_id, other):  # an earlier decision's event, since replaced
        d.service._orch._audit.record(rid, "policy.decided", "gawe.orchestrator", {"old": rid})
    run = d.stores.runs.get(run_id)
    step = d.stores.steps.get(run_id, "rate")
    refreshed = MaintenanceJob(
        job_id="e" * 64,
        kind="write_readback",
        tenant_id="t1",
        run_id=run_id,
        plan_version=run.plan_version,
        step_id="rate",
        action_hash=step.action_hash,
        operation_key="refreshed",
        created_at=d.clock.now,
        deadline=d.clock.now,
        due_at=d.clock.now,
        result={"outcome": "recipient_changed", "code": "recipient_changed"},
        continued=True,
    )
    d.stores.maintenance.add(refreshed)
    d.review()
    assert d.reasons() == [(run_id, None, "superseded"), (other, None, "audit_mismatch")]


# --- re-evaluation ------------------------------------------------------------------------------


def stricter_policy(d: Desk) -> Path:
    text = d.bundle().policy_rules.read_text(encoding="utf-8")
    path = d.path / "stricter.yaml"
    path.write_text(
        text
        + """
  - id: every-rating-needs-legal
    effect: needs_approval
    approver_role: legal_reviewer
    when: {tool: crm.write_assessment}
""",
        encoding="utf-8",
    )
    return path


def test_rules_changed_is_labelled_by_the_rule_set_digest(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    run_id = d.run()
    d.restart(policy=stricter_policy(d))
    d.review()
    assert d.reasons() == [(run_id, "rate", "rules_changed")]


def test_inputs_changed_when_the_same_rules_read_a_different_value(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    run_id = d.run()
    d.clm.replace_contract("C-1001", {"deal_value": 900_000})  # now a large deal
    d.review()
    assert d.reasons() == [(run_id, "rate", "inputs_changed")]


def test_a_decision_from_before_digests_says_rules_may_have_changed(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    policy = d.service._orch._policy
    original = policy.evaluate

    def as_v1(ctx: Any) -> PolicyDecision:  # the engine as it was before M16b
        made = original(ctx)
        return seal_v1(
            step_id=made.step_id,
            action_hash=made.action_hash,
            outcome=made.outcome,
            rule_ids=made.rule_ids,
            reason=made.reason,
            approver_role=made.approver_role,
        )

    policy.evaluate = as_v1  # type: ignore[method-assign]
    run_id = d.run()
    assert d.stores.steps.get(run_id, "rate").policy_decision.format == 1  # type: ignore[union-attr]
    d.restart(policy=stricter_policy(d))
    d.review()
    assert d.reasons() == [(run_id, "rate", "rules_may_have_changed")]


def test_denied_or_approved_actions_are_not_re_evaluated(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    d.provider._script.append({"steps": [rate_step({**LOW, "contract_id": "C-1002"})]})
    d.service._orch.start_run(
        tenant_id="t1",
        acting_user_id="alice",
        roles=("case_manager",),
        objective="Rate a large deal",
        profile="contract_review",
        run_id="large",
    )
    assert d.service._orch.advance("large").status is RunStatus.PAUSED_FOR_APPROVAL
    d.restart(policy=stricter_policy(d))
    d.review()
    assert d.items() == []  # needs_approval, never executed automatically


# --- the sample -----------------------------------------------------------------------------------


def test_sample_is_reproducible_with_the_key_and_changes_with_another() -> None:
    hashes = [token_hex(32) for _ in range(400)]
    a, again, other = key(), key(), ReviewKey(bytes(range(1, 33)))
    picked = [h for h in hashes if a.selects(h)]
    assert picked == [h for h in hashes if again.selects(h)]
    assert picked != [h for h in hashes if other.selects(h)]
    assert a.key_id == again.key_id != other.key_id and len(a.key_id) == 16


def test_sample_lands_near_five_percent_on_synthetic_volume() -> None:
    k = ReviewKey(token_bytes(32))
    picked = sum(k.selects(token_hex(32)) for _ in range(20_000))
    assert 850 <= picked <= 1150  # 5% of 20,000 is 1,000; this is about +/- 5 standard deviations


def test_the_sample_key_is_long_hex_and_not_the_signing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        ReviewKey(b"short")
    with pytest.raises(ValueError, match="differ from the policy signing key"):
        ReviewKey(KEY, signing_key=KEY)
    monkeypatch.setenv("GAWE_POLICY_SIGNING_KEY", KEY.hex())
    monkeypatch.setenv("GAWE_REVIEW_SAMPLE_KEY", KEY.hex())
    with pytest.raises(ValueError, match="differ"):
        ReviewKey.from_env()
    monkeypatch.setenv("GAWE_REVIEW_SAMPLE_KEY", "not hex")
    with pytest.raises(ValueError, match="hex"):
        ReviewKey.from_env()
    monkeypatch.delenv("GAWE_REVIEW_SAMPLE_KEY")
    assert ReviewKey.from_env() is None
    monkeypatch.setenv("GAWE_REVIEW_SAMPLE_KEY", bytes(range(100, 132)).hex())
    loaded = ReviewKey.from_env()
    assert loaded is not None and loaded.key_id == key().key_id


def test_low_ratings_with_nothing_flagged_are_drawn_and_record_the_key_id(tmp_path: Path) -> None:
    d = Desk(tmp_path, key=key(), rate=ALWAYS)
    run_id = d.run()
    beat = d.review()
    (item,) = d.items()
    assert (item.run_id, item.reason, item.key_id) == (run_id, "sampled_low", key().key_id)
    assert item.action_hash == d.stores.steps.get(run_id, "rate").action_hash
    assert beat.outcomes["reviewed"] == 7  # the first tick reviews the last 7 days
    (event,) = d.events(run_id, "review.sampled")
    assert item.event_seq == event.seq


def test_only_low_ratings_with_nothing_flagged_are_candidates(tmp_path: Path) -> None:
    d = Desk(tmp_path, key=key(), rate=ALWAYS)
    d.run(step=rate_step({**LOW, "risk_level": "medium"}))
    flagged = d.run()
    d.stores.runs.update(
        flagged, findings={"flagged_clauses": [{"kind": "other", "severity": "low"}]}
    )
    d.review()
    assert d.items() == []


def test_with_rate_zero_nothing_is_drawn(tmp_path: Path) -> None:
    d = Desk(tmp_path, key=key(), rate=0.0)
    d.run()
    d.review()
    assert d.items() == []


# --- recording ---------------------------------------------------------------------------------


def test_items_and_events_are_recorded_once_and_the_anchor_is_refreshed(tmp_path: Path) -> None:
    d = Desk(tmp_path, key=key(), rate=ALWAYS)
    run_id = d.run()
    anchored = d.sink.get(run_id)
    assert anchored is not None
    d.review()
    handler = next(h for h in d.runner._handlers.values() if h.kind == KIND)
    handler.review("t1", DAY)  # a second pass over the same day (a crash, a second sweeper)
    assert len(d.items()) == 1 and len(d.events(run_id, "review.sampled")) == 1
    events = d.stores.audit.events(run_id)
    assert verify_chain(events).ok and events[-1].type == "review.sampled"
    refreshed = d.sink.get(run_id)
    assert refreshed is not None and refreshed.point.seq == events[-1].seq > anchored.point.seq


def test_a_review_never_changes_a_run_or_a_decision(tmp_path: Path) -> None:
    d = Desk(tmp_path, key=key(), rate=ALWAYS)
    run_id = d.run()
    before = (d.stores.runs.get(run_id), d.stores.steps.get(run_id, "rate"))
    d.restart(policy=stricter_policy(d))
    d.review()
    assert (d.stores.runs.get(run_id), d.stores.steps.get(run_id, "rate")) == before


# --- tenants and verdicts ---------------------------------------------------------------------


def test_each_tenant_sees_only_its_own_items(tmp_path: Path) -> None:
    d = Desk(tmp_path, key=key(), rate=ALWAYS)
    mine, theirs = d.run(ALICE), d.run(CAROL)
    d.review()
    assert [i.run_id for i in d.service.review_items(BOB)] == [mine]
    assert [i.run_id for i in d.service.review_items(DAN)] == [theirs]
    (their_item,) = d.service.review_items(DAN)
    with pytest.raises(NotFoundError):
        d.service.review_verdict(BOB, their_item.item_id, verdict="confirmed")


def test_verdicts_need_a_reviewer_who_did_not_start_the_run(tmp_path: Path) -> None:
    d = Desk(tmp_path, key=key(), rate=ALWAYS)
    d.run(ALICE)
    d.review()
    with pytest.raises(ForbiddenError, match="legal_reviewer"):
        d.service.review_items(ALICE)
    (item,) = d.service.review_items(BOB)
    with pytest.raises(ForbiddenError, match="started a run"):
        d.service.review_verdict(REVIEWING_REQUESTER, item.item_id, verdict="confirmed")
    decided = d.service.review_verdict(BOB, item.item_id, verdict="missed_risk")
    assert (decided.status, decided.decided_by) == ("missed_risk", "bob")
    with pytest.raises(ConflictError):
        d.service.review_verdict(BOB, item.item_id, verdict="confirmed")
    assert d.service.review_items(BOB) == []  # open items only, by default
    assert [i.status for i in d.service.review_items(BOB, status=None)] == ["missed_risk"]
    with pytest.raises(NotFoundError):
        d.service.review_verdict(BOB, "0" * 64, verdict="confirmed")


def test_review_events_carry_no_argument_values(tmp_path: Path) -> None:
    d = Desk(tmp_path, key=key(), rate=ALWAYS)
    run_id = d.run()
    args = d.stores.steps.get(run_id, "rate").resolved_args
    payloads: list[dict[str, Any]] = []
    handler = next(h for h in d.runner._handlers.values() if h.kind == KIND)
    original = handler._audit.record

    def capture(rid: str, type: str, actor: str, payload: Any = None) -> Any:
        if type.startswith("review."):
            payloads.append(dict(payload))
        return original(rid, type, actor, payload)

    handler._audit.record = capture  # type: ignore[attr-defined]
    d.review()
    (payload,) = payloads
    assert set(payload) == {"item_id", "day", "step_id", "action_hash", "reason", "key_id"}
    text = str(payload) + str([i.model_dump() for i in d.items()])
    for value in (args["summary"], args["record"]["contract_version"]):
        assert str(value) not in text
