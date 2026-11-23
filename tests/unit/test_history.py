"""M24 S1: an app's own history, read safely, for rules that only escalate.

The test app assesses contracts; its rule `repeat-assessment-needs-legal` makes a second
assessment of one contract wait for legal, whether the first was in this run or another.
"""

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.api_bundle import bundle
from tests.api_flow import ALICE, BOB, CAROL
from tests.flow import FIXTURES
from tests.helpers import signer

from gawe.api.background import InlineBackground
from gawe.api.wiring import AppBundle, Stores, assemble, load_app_rules, memory_stores
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.history import (
    History,
    HistoryEntry,
    HistorySubject,
    InMemoryHistoryReader,
    InMemorySubjectLocks,
    counts,
)
from gawe.core.models import Plan, PlanStep
from gawe.core.policy.provenance import PolicySource, PolicyVariable
from gawe.core.policy.rules import RuleConfigError
from gawe.core.records import InMemoryStepStore, StepRecord
from gawe.core.runs import InMemoryRunStore, RunRecord
from gawe.core.statemachine import StepStatus
from gawe.llm.fake import FakeProvider

TOOL = "crm.write_assessment"
H = "a" * 64


def assessed(entries: Any, args: Any, run: Any) -> dict[str, Any]:
    return {"assessed": [args["contract_id"]] if entries else []}


SUBJECT = HistorySubject(argument="contract_id", variables=assessed, shown=("risk_score",))


def history_bundle(**changes: Any) -> AppBundle:
    b, _ = bundle()
    b = replace(
        b,
        policy_rules=FIXTURES / "history_policies.yaml",
        history={TOOL: SUBJECT},
        policy_sources={
            **b.policy_sources,
            "assessment_history": PolicySource(kind="history", resource=f"gawe.history.{TOOL}"),
        },
        policy_variables={
            **b.policy_variables,
            "history.assessed": PolicyVariable(dependencies=("assessment_history",)),
        },
    )
    return replace(b, **changes)


def assess(*contracts: str, score: int = 3) -> dict[str, Any]:
    return {
        "steps": [
            {
                "id": f"w{i}",
                "tool": TOOL,
                "args": {"contract_id": c, "risk_score": score},
                "depends_on": [f"w{i - 1}"] if i else [],
            }
            for i, c in enumerate(contracts)
        ]
    }


class App:
    def __init__(self, plans: list[dict[str, Any]], stores: Stores | None = None) -> None:
        self.stores = stores or memory_stores()
        self.service = assemble(
            history_bundle(),
            stores=self.stores,
            signer=signer(),
            provider=FakeProvider(plans),
            background=InlineBackground(),
        )

    def run(self, who: Any = ALICE) -> Any:
        started = self.service.start_run(who, objective="assess", profile="contract_review")
        return self.service.get_run(who, started.run_id)


# --- end to end ---------------------------------------------------------------------------------


def test_a_second_assessment_of_one_contract_waits_and_another_contract_doesnt() -> None:
    app = App([assess("C-1"), assess("C-1"), assess("C-2")])
    assert app.run().status == "completed"
    second = app.run()
    assert second.status == "paused_for_approval"
    assert "repeat-assessment-needs-legal" in second.steps[-1].policy_rule_ids
    assert app.run().status == "completed"  # C-2 has no history
    (item,) = app.service.inbox(BOB)
    app.service.decide(BOB, item.approval_id, decision="approved", action_hash=item.action_hash)
    assert app.service.get_run(ALICE, second.run_id).status == "completed"


def test_an_earlier_step_of_the_same_run_counts() -> None:
    app = App([assess("C-1", "C-1")])
    view = app.run()
    assert view.status == "paused_for_approval"
    assert view.steps[0].status == "succeeded"
    assert "repeat-assessment-needs-legal" in view.steps[1].policy_rule_ids


def test_another_tenants_history_doesnt_count() -> None:
    app = App([assess("C-1"), assess("C-1")])
    assert app.run(ALICE).status == "completed"
    assert app.run(CAROL).status == "completed"  # another tenant, same contract id


def test_the_approver_sees_what_matched_and_the_audit_only_references_it() -> None:
    app = App([assess("C-1", score=7), assess("C-1")])
    first = app.run()
    second = app.run()
    step = app.stores.steps.get(second.run_id, "w0")
    started = app.stores.runs.get(first.run_id).created_at
    assert step.preparation["history"] == [
        {
            "tool": TOOL,
            "run_id": first.run_id,
            "step_id": "w0",
            "run_started_at": started.isoformat(),
            "values": {"risk_score": 7},
        }
    ]
    (item,) = app.service.inbox(BOB)
    assert [(h.run_id, h.values) for h in item.history] == [(first.run_id, {"risk_score": 7})]
    counted = app.stores.steps.get(first.run_id, "w0")
    events = [e for e in app.stores.audit.events(second.run_id) if e.type == "history.read"]
    payload = {
        "step_id": "w0",
        "tool": TOOL,
        "count": 1,
        "counted": [[first.run_id, "w0", counted.action_hash]],
    }
    assert [e.payload_hash for e in events] == [sha256_hex(canonical_json(payload))]


def test_unreadable_history_denies() -> None:
    stores = memory_stores()

    class Broken:
        def earlier(self, *args: Any) -> list[HistoryEntry]:
            raise OSError("down")

    app = App([assess("C-1")], replace(stores, history=Broken()))
    view = app.run()
    assert view.status == "denied" and "engine.error" in view.steps[0].policy_rule_ids
    assert [e.type for e in app.stores.audit.events(view.run_id)].count("history.read") == 1


def test_two_runs_at_once_dont_both_see_nothing_before() -> None:
    stores = memory_stores()
    reader = stores.history
    assert reader is not None

    class Slow:
        def earlier(self, *args: Any) -> list[HistoryEntry]:
            found = reader.earlier(*args)
            time.sleep(0.2)  # without the lock, the other run reads in this gap
            return found

    app = App([assess("C-1"), assess("C-1")], replace(stores, history=Slow()))
    views: list[Any] = []
    threads = [threading.Thread(target=lambda: views.append(app.run())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(v.status for v in views) == ["completed", "paused_for_approval"]


# --- what counts --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "counted"),
    [
        (StepStatus.POLICY_CHECKED, True),
        (StepStatus.AWAITING_APPROVAL, True),
        (StepStatus.EXECUTING, True),
        (StepStatus.VERIFICATION_UNKNOWN, True),
        (StepStatus.SUCCEEDED, True),
        (StepStatus.FAILED, True),
        (StepStatus.PREPARED, True),
        (StepStatus.DONE_BY_PERSON, True),
        (StepStatus.DENIED, False),
        (StepStatus.REJECTED, False),
        (StepStatus.NOT_DONE_BY_PERSON, False),
    ],
)
def test_what_counts_as_an_earlier_action(status: StepStatus, counted: bool) -> None:
    step = StepRecord(
        run_id="r", step_id="s", tool=TOOL, tool_version="1.0.0", action_hash=H, status=status
    )
    assert counts(step) is counted


def test_a_step_that_failed_before_policy_doesnt_count() -> None:
    step = StepRecord(run_id="r", step_id="s", tool=TOOL, tool_version="1.0.0", status="failed")
    assert not counts(step)


T0 = datetime(2026, 11, 1, tzinfo=UTC)


def world() -> tuple[InMemoryRunStore, InMemoryStepStore, History]:
    runs, steps = InMemoryRunStore(), InMemoryStepStore()
    history = History(
        {TOOL: HistorySubject(argument="contract_id", variables=assessed, lower=True)},
        InMemoryHistoryReader(runs, steps),
        InMemorySubjectLocks(),
    )
    return runs, steps, history


def add_run(
    runs: InMemoryRunStore,
    steps: InMemoryStepStore,
    run_id: str,
    started: datetime,
    contracts: list[str],
    *,
    tenant: str = "t1",
    version: str = "1.0.0",
) -> RunRecord:
    plan = Plan(
        steps=tuple(
            PlanStep(id=f"s{i}", tool=TOOL, args={"contract_id": c})
            for i, c in enumerate(contracts)
        )
    )
    run = RunRecord(
        run_id=run_id,
        tenant_id=tenant,
        acting_user_id="u",
        objective="o",
        profile="p",
        created_at=started,
        plan=plan,
        plan_version=1,
    )
    runs.add(run)
    for i, c in enumerate(contracts):
        steps.add(
            StepRecord(
                run_id=run_id,
                step_id=f"s{i}",
                tool=TOOL,
                tool_version=version,
                resolved_args={"contract_id": c, "risk_score": 1},
                action_hash=sha256_hex(f"{run_id}{i}".encode()),
                status=StepStatus.SUCCEEDED,
            )
        )
    return run


def test_history_reads_the_same_subject_any_version_this_tenant_and_earlier_plan_steps() -> None:
    runs, steps, history = world()
    add_run(runs, steps, "r1", T0, ["C-1"], version="0.9.0")
    add_run(runs, steps, "r2", T0 + timedelta(days=1), ["c-1"])  # compared lower-cased
    add_run(runs, steps, "r3", T0, ["C-1"], tenant="t2")
    add_run(runs, steps, "r4", T0, ["C-2"])
    judged = add_run(runs, steps, "r5", T0 + timedelta(days=2), ["C-1", "C-1", "C-1"])
    read = history.read(judged, "s2", TOOL, {"contract_id": "C-1"})
    assert [(e.run_id, e.step_id) for e in read.entries] == [
        ("r1", "s0"),
        ("r2", "s0"),
        ("r5", "s0"),
        ("r5", "s1"),
    ]
    assert read.variables == {"assessed": ["C-1"]}


def test_this_runs_steps_come_last_in_plan_order_and_the_card_shows_the_latest_few() -> None:
    runs, steps, history = world()
    for day in range(12):
        add_run(runs, steps, f"r{day:02}", T0 + timedelta(days=day), ["C-1"])
    judged = add_run(runs, steps, "z", T0 + timedelta(days=20), ["C-1"] * 11)
    read = history.read(judged, "s10", TOOL, {"contract_id": "C-1"})
    assert len(read.entries) == 22
    assert [e.step_id for e in read.entries[12:]] == [f"s{i}" for i in range(10)]
    assert [s["step_id"] for s in read.shown] == [f"s{i}" for i in range(10)]
    assert read.audit_payload("s10", TOOL)["count"] == 22


def test_history_as_of_a_runs_start_leaves_later_runs_out() -> None:
    runs, steps, history = world()
    judged = add_run(runs, steps, "r1", T0, ["C-1"])
    add_run(runs, steps, "r2", T0 + timedelta(days=1), ["C-1"])
    assert [e.run_id for e in history.read(judged, "s0", TOOL, {"contract_id": "C-1"}).entries] == [
        "r2"
    ]
    assert history.read(judged, "s0", TOOL, {"contract_id": "C-1"}, as_of=T0).entries == ()


# --- startup ------------------------------------------------------------------------------------


def test_an_allow_rule_reading_history_is_refused(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    text = (FIXTURES / "history_policies.yaml").read_text(encoding="utf-8")
    rules.write_text(
        text.replace(
            "effect: needs_approval\n    approver_role: legal_reviewer\n"
            "    reason: This contract was assessed before",
            "effect: allow\n    reason: This contract was assessed before",
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuleConfigError, match="history may only escalate"):
        load_app_rules(history_bundle(policy_rules=rules))


@pytest.mark.parametrize(
    "dependencies",
    [("tenant_config",), ("assessment_history", "reader_findings")],
)
def test_a_history_variable_must_read_history_and_otherwise_settings_only(
    dependencies: tuple[str, ...],
) -> None:
    b = history_bundle()
    b = replace(
        b,
        policy_sources={
            **b.policy_sources,
            "reader_findings": PolicySource(kind="findings", resource="run.findings"),
        },
        policy_variables={
            **b.policy_variables,
            "history.assessed": PolicyVariable(dependencies=dependencies),
        },
    )
    with pytest.raises(ValueError, match="must read a history source"):
        load_app_rules(b)


def test_a_history_variable_may_also_read_settings() -> None:
    b = history_bundle()
    deps = ("assessment_history", "tenant_config")
    load_app_rules(
        replace(
            b,
            policy_variables={
                **b.policy_variables,
                "history.assessed": PolicyVariable(dependencies=deps),
            },
        )
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"history": {"no.such_tool": SUBJECT}}, "unknown tool"),
        ({"history": {TOOL: replace(SUBJECT, argument="nope")}}, "no argument 'nope'"),
        ({"history": {TOOL: replace(SUBJECT, shown=("nope",))}}, "isn't a typed field"),
    ],
)
def test_history_must_name_a_tool_and_one_of_its_arguments(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_app_rules(history_bundle(**change))


def test_a_history_source_must_name_declared_history() -> None:
    b = history_bundle()
    sources = {
        **b.policy_sources,
        "assessment_history": PolicySource(kind="history", resource="gawe.history.other.tool"),
    }
    with pytest.raises(ValueError, match="names no declared history"):
        load_app_rules(replace(b, policy_sources=sources))


def test_stores_without_a_history_reader_refuse_an_app_that_reads_history() -> None:
    with pytest.raises(ValueError, match="no history reader"):
        App([], replace(memory_stores(), history=None))
