"""A fully wired in-memory governance flow for executor and adversarial tests."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gawe.core.approvals import ApprovalService, InMemoryApprovalStore, Principal
from gawe.core.audit import AuditLog, InMemoryAuditStore
from gawe.core.executor import Executor, RetryPolicy, ToolCall, ToolImplementation
from gawe.core.models import PolicyDecision, ToolManifest
from gawe.core.policy.engine import RuleEngine
from gawe.core.policy.rules import load_rules
from gawe.core.records import InMemoryExecutionStore, InMemoryStepStore, StepRecord
from gawe.core.registry import ToolRegistry
from gawe.core.statemachine import StepStatus
from gawe.core.verifier import PostCondition, Verifier
from tests.helpers import TOOLS, make_ctx, signer

FIXTURES = Path(__file__).parent / "fixtures"
RUN = "run1"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ALICE = Principal("alice", frozenset({"case_manager"}))
BOB = Principal("bob", frozenset({"legal_reviewer"}))


class Crash(BaseException):
    """Simulates the process dying mid-call (not catchable by `except Exception`)."""


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class FakeConnector:
    """An idempotent external system: repeated calls with one key cause one effect."""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []
        self.effects: dict[str, Mapping[str, Any]] = {}
        self.script: list[BaseException | None] = []  # consumed one per call; None = succeed

    def __call__(self, args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        self.calls.append(call)
        if self.script:
            problem = self.script.pop(0)
            if problem is not None:
                raise problem
        return self.effects.setdefault(
            call.idempotency_key, {"message_id": f"m-{len(self.effects)}"}
        )


def manifests() -> dict[str, ToolManifest]:
    out = {name: m for name, m in TOOLS.items()}
    send = out["contracts.send_email"]
    out["contracts.send_email"] = send.model_copy(
        update={
            "output_schema": {
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
            }
        }
    )
    return out


class Flow:
    def __init__(
        self,
        *,
        retry: RetryPolicy | None = None,
        postconditions: Mapping[str, PostCondition] | None = None,
        tool_overrides: Mapping[str, ToolImplementation] | None = None,
        lease: timedelta = timedelta(minutes=5),
    ) -> None:
        self.clock = Clock()
        self.sleeps: list[float] = []
        self.audit_store = InMemoryAuditStore()
        self.audit = AuditLog(self.audit_store)
        self.steps = InMemoryStepStore()
        self.executions = InMemoryExecutionStore()
        self.registry = ToolRegistry()
        for manifest in manifests().values():
            self.registry.register(manifest)
        self.engine = RuleEngine(
            load_rules(FIXTURES / "example_policies.yaml", manifests=tuple(TOOLS.values())),
            signer(),
        )
        self.approval_store = InMemoryApprovalStore()
        self.approvals = ApprovalService(
            self.approval_store,
            signer(),
            self.audit,
            clock=self.clock,
            default_ttl=timedelta(hours=1),
        )
        self.connector = FakeConnector()
        tools: dict[str, ToolImplementation] = {name: self.connector for name in TOOLS}
        tools.update(tool_overrides or {})
        self.executor = Executor(
            steps=self.steps,
            executions=self.executions,
            registry=self.registry,
            tools=tools,
            signer=signer(),
            approvals=self.approvals,
            audit=self.audit,
            verifier=Verifier(postconditions),
            retry=retry or RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=8.0),
            lease=lease,
            clock=self.clock,
            sleep=self.sleeps.append,
        )

    def judge(
        self,
        tool: str,
        args: Mapping[str, Any],
        *,
        step_id: str = "s1",
        roles: tuple[str, ...] = ("case_manager",),
    ) -> PolicyDecision:
        manifest = self.registry.get(tool)
        return self.engine.evaluate(
            make_ctx(manifest, args, roles=roles, run_id=RUN, step_id=step_id)
        )

    def add_step(
        self,
        decision: PolicyDecision,
        tool: str,
        args: Mapping[str, Any],
        *,
        status: StepStatus | None = None,
    ) -> StepRecord:
        """Insert a step in the state the orchestrator would leave it in after policy."""
        if status is None:
            status = (
                StepStatus.AWAITING_APPROVAL
                if decision.outcome == "needs_approval"
                else StepStatus.POLICY_CHECKED
            )
        record = StepRecord(
            run_id=RUN,
            step_id=decision.step_id,
            tool=tool,
            tool_version="1.0.0",
            resolved_args=dict(args),
            action_hash=decision.action_hash,
            status=status,
        )
        self.steps.add(record)
        return record

    def approve(self, decision: PolicyDecision, *, approver: Principal = BOB) -> None:
        record = self.approvals.request(run_id=RUN, decision=decision, requested_by=ALICE.user_id)
        self.approvals.decide(
            record.approval_id,
            decider=approver,
            decision="approved",
            action_hash=decision.action_hash,
        )
        self.steps.transition(
            RUN, decision.step_id, expected=StepStatus.AWAITING_APPROVAL, to=StepStatus.APPROVED
        )

    def audit_types(self) -> list[str]:
        return [e.type for e in self.audit_store.events(RUN)]


INTERNAL_EMAIL = {"to": "colleague@acme.com", "body": "hello"}
EXTERNAL_EMAIL = {"to": "counsel@partner.com", "body": "redlines attached"}
