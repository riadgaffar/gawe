"""An in-memory orchestrator wired to fake LLM providers, for engine tests."""

from collections.abc import Callable, Mapping
from typing import Any

from gawe.core.approvals import Principal
from gawe.core.runs import InMemoryRunStore, RunRecord
from gawe.core.telemetry import InMemoryTelemetry
from gawe.engine.orchestrator import Orchestrator
from gawe.llm.fake import FakeProvider
from gawe.llm.planner import Planner
from gawe.llm.reader import Reader
from tests.flow import ALICE, BOB, Flow
from tests.unit.test_llm_reader import PROFILE

TENANT_VARS: dict[str, Any] = {"tenant": {"internal_domains": ["acme.com"]}}
GOOD_READ = {"risk": "high", "summary": "Unlimited liability clause"}


def plan_of(*steps: Mapping[str, Any]) -> dict[str, Any]:
    return {"steps": [dict(s) for s in steps]}


def step(
    sid: str, tool: str, args: Mapping[str, Any], deps: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {"id": sid, "tool": tool, "args": dict(args), "depends_on": list(deps)}


class EngineFlow(Flow):
    def __init__(
        self,
        plan_script: list[Any],
        read_script: list[Any] | None = None,
        on_finished: Callable[[RunRecord], object] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.runs = InMemoryRunStore()
        self.telemetry = InMemoryTelemetry()
        self.planner_llm = FakeProvider(plan_script)
        self.reader_llm = FakeProvider(read_script or [])
        self.orch = Orchestrator(
            runs=self.runs,
            steps=self.steps,
            executions=self.executions,
            registry=self.registry,
            policy=self.engine,
            executor=self.executor,
            approvals=self.approvals,
            planner=Planner(self.planner_llm, self.registry),
            reader=Reader(self.reader_llm, PROFILE) if read_script is not None else None,
            audit=self.audit,
            telemetry=self.telemetry,
            variables=lambda run: TENANT_VARS,
            on_finished=on_finished,
            clock=self.clock,
        )

    def start(self, document: str | None = None, run_id: str = "run1") -> RunRecord:
        return self.orch.start_run(
            tenant_id="t1",
            acting_user_id=ALICE.user_id,
            roles=tuple(ALICE.roles),
            objective="Review the contract and email counsel",
            profile="contract_review",
            document=document,
            run_id=run_id,
        )

    def approve_open(self, approver: Principal = BOB, run_id: str = "run1") -> None:
        for record in self.approval_store.open_approvals():
            if record.run_id == run_id:
                self.approvals.decide(
                    record.approval_id,
                    decider=approver,
                    decision="approved",
                    action_hash=record.action_hash,
                )

    def reject_open(self, run_id: str = "run1") -> None:
        for record in self.approval_store.open_approvals():
            if record.run_id == run_id:
                self.approvals.decide(
                    record.approval_id,
                    decider=BOB,
                    decision="rejected",
                    action_hash=record.action_hash,
                )

    def step_status(self, sid: str, run_id: str = "run1") -> str:
        return self.steps.get(run_id, sid).status.value


EMAIL_PLAN = plan_of(
    step("risks", "contracts.extract_risks", {"document_id": "d1"}),
    step(
        "email",
        "contracts.send_email",
        {"to": "counsel@partner.com", "body": "redlines attached"},
        ("risks",),
    ),
)
INTERNAL_PLAN = plan_of(
    step("risks", "contracts.extract_risks", {"document_id": "d1"}),
    step(
        "assess",
        "crm.write_assessment",
        {"contract_id": "c1", "risk_score": 3},
        ("risks",),
    ),
)
