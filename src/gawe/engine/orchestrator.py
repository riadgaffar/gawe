"""Drives a run from objective to a terminal state.

`advance(run_id)` is re-entrant and idempotent: it does as much as it can, then stops because the
run finished or is waiting for a human. After an approval is decided, call it again. All progress
lives in the stores, so a crashed process loses nothing and a restarted one continues:

* the plan is saved before any step exists, so a restart never re-plans;
* every step moves through the step state machine, one compare-and-set at a time;
* the signed policy decision for each step is saved, so an approved step runs under exactly the
  decision a human approved;
* only the executor runs tools, and it re-checks authorization itself.

Steps run one at a time in plan order, each as soon as its dependencies have succeeded. Any policy
denial or human rejection ends the run as `denied`; any step failure ends it as `failed`. Steps
that can no longer run are failed with a reason rather than left dangling.

Audit events and telemetry carry ids, hashes and status codes only, never prompts, document text,
tool arguments or outputs.

`on_finished` is called once a run has ended and its last audit event is written (after open
approvals are closed), which is where the audit head gets anchored. It can never fail the run.
"""

import contextlib
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from gawe.core.approvals import ApprovalAlreadyOpenError, ApprovalService
from gawe.core.audit import AuditLog
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.executor import ConcurrentExecutionError, ExecutionRefusedError, Executor
from gawe.core.models import PolicyDecision
from gawe.core.policy.engine import PolicyEngine
from gawe.core.policy.rules import EvaluationContext
from gawe.core.records import ExecutionStore, StaleStateError, StepRecord, StepStore
from gawe.core.refs import RefResolutionError, resolve_args
from gawe.core.registry import ToolRegistry
from gawe.core.runs import RunRecord, RunStore
from gawe.core.statemachine import STEP_TERMINAL, RunStatus, StepStatus
from gawe.core.telemetry import NoopTelemetry, Telemetry
from gawe.llm.planner import Planner, PlanningError
from gawe.llm.provider import LLMUnavailableError
from gawe.llm.reader import Findings, Reader, ReaderError

log = logging.getLogger("gawe.engine")

ACTOR = "gawe.orchestrator"
_RUN_TERMINAL = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.DENIED}
)
_IN_FLIGHT = frozenset({StepStatus.EXECUTING, StepStatus.VERIFYING})
_BAD_UPSTREAM = frozenset({StepStatus.FAILED, StepStatus.DENIED, StepStatus.REJECTED})


class RunBusyError(Exception):
    """The run cannot be changed right now (a step is mid-execution)."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Orchestrator:
    def __init__(
        self,
        *,
        runs: RunStore,
        steps: StepStore,
        executions: ExecutionStore,
        registry: ToolRegistry,
        policy: PolicyEngine,
        executor: Executor,
        approvals: ApprovalService,
        planner: Planner,
        audit: AuditLog,
        reader: Reader | None = None,
        telemetry: Telemetry | None = None,
        variables: Callable[[RunRecord], Mapping[str, Any]] | None = None,
        on_finished: Callable[[RunRecord], object] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._runs = runs
        self._steps = steps
        self._executions = executions
        self._registry = registry
        self._policy = policy
        self._executor = executor
        self._approvals = approvals
        self._planner = planner
        self._reader = reader
        self._audit = audit
        self._telemetry: Telemetry = telemetry or NoopTelemetry()
        self._variables = variables or (lambda run: {})
        self._on_finished = on_finished
        self._clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- public ---------------------------------------------------------------------------

    def start_run(
        self,
        *,
        tenant_id: str,
        acting_user_id: str,
        roles: tuple[str, ...],
        objective: str,
        profile: str,
        document: str | None = None,
        run_id: str | None = None,
    ) -> RunRecord:
        if document is not None and self._reader is None:
            raise ValueError("a document was provided but no Reader is configured")
        run = RunRecord(
            run_id=run_id or str(uuid4()),
            tenant_id=tenant_id,
            acting_user_id=acting_user_id,
            roles=roles,
            objective=objective,
            profile=profile,
            document=document,
            created_at=self._clock(),
        )
        self._runs.add(run)
        self._audit.record(
            run.run_id,
            "run.created",
            acting_user_id,
            {
                "profile": profile,
                "has_document": document is not None,
                "objective_sha256": sha256_hex(objective.encode("utf-8")),
            },
        )
        return run

    def get(self, run_id: str) -> RunRecord:
        return self._runs.get(run_id)

    def advance(self, run_id: str) -> RunRecord:
        lock = self._lock_for(run_id)
        if not lock.acquire(blocking=False):
            return self._runs.get(run_id)  # another driver is already advancing this run
        try:
            with self._telemetry.span("gawe.run.advance", run_id=run_id) as span:
                run = self._runs.get(run_id)
                if run.status in (RunStatus.CREATED, RunStatus.PLANNING):
                    run = self._plan(run)
                if run.status in (RunStatus.RUNNING, RunStatus.PAUSED_FOR_APPROVAL):
                    run = self._execute(run)
                span.set("status", run.status.value)
                return run
        finally:
            lock.release()

    def cancel(self, run_id: str, *, by: str) -> RunRecord:
        with self._lock_for(run_id):
            run = self._runs.get(run_id)
            if run.status in _RUN_TERMINAL:
                return run
            plan_steps = run.plan.steps if run.plan else ()
            current = [self._steps.get(run_id, s.id) for s in plan_steps]
            if any(s.status in _IN_FLIGHT for s in current):
                raise RunBusyError("a step is executing; try again when it has finished")
            for step in current:
                self._fail_step(run_id, step.step_id, "cancelled")
            run = self._runs.transition(
                run_id,
                expected=run.status,
                to=RunStatus.CANCELLED,
                completed_at=self._clock(),
                document=None,
            )
            self._audit.record(run_id, "run.cancelled", by, {})
            self._approvals.close_for_run(run_id, reason="run_cancelled")
            self._record_outcome(run)
            self._notify_finished(run)
            return run

    # -- planning -------------------------------------------------------------------------

    def _plan(self, run: RunRecord) -> RunRecord:
        if run.status is RunStatus.CREATED:
            run = self._runs.transition(
                run.run_id, expected=RunStatus.CREATED, to=RunStatus.PLANNING
            )
            self._audit.record(run.run_id, "run.planning", ACTOR, {})
        if run.plan is None:
            try:
                with self._telemetry.span("gawe.plan", run_id=run.run_id):
                    findings = self._read(run)
                    result = self._planner.plan(run.objective, findings)
            except LLMUnavailableError:
                self._audit.record(run.run_id, "run.planning_deferred", ACTOR, {})
                self._telemetry.count("gawe.plan.attempts", valid=False, first_try=True)
                return self._runs.get(run.run_id)
            except ReaderError:
                return self._finish(run, RunStatus.FAILED, "reading_failed")
            except PlanningError:
                self._telemetry.count("gawe.plan.attempts", valid=False, first_try=False)
                return self._finish(run, RunStatus.FAILED, "planning_failed")
            run = self._runs.update(
                run.run_id, plan=result.plan, plan_version=1, planner_model=result.model
            )
            self._telemetry.count("gawe.plan.attempts", valid=True, first_try=result.attempts == 1)
            self._audit.record(
                run.run_id,
                "llm.plan",
                ACTOR,
                {
                    "model": result.model,
                    "attempts": result.attempts,
                    "request_hashes": list(result.request_hashes),
                    "response_hashes": list(result.response_hashes),
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                },
            )
            self._audit.record(
                run.run_id,
                "plan.created",
                ACTOR,
                {
                    "version": 1,
                    "steps": len(result.plan.steps),
                    "plan_sha256": sha256_hex(canonical_json(result.plan.model_dump())),
                },
            )
        self._materialize(run)
        return self._runs.transition(run.run_id, expected=RunStatus.PLANNING, to=RunStatus.RUNNING)

    def _read(self, run: RunRecord) -> Findings | None:
        if run.findings is not None and run.findings_schema_id is not None:
            return Findings(run.findings_schema_id, run.findings)
        if run.document is None or self._reader is None:
            return None
        result = self._reader.read(run.document)
        self._runs.update(
            run.run_id,
            findings=dict(result.findings.data),
            findings_schema_id=result.findings.schema_id,
            document=None,  # the document is not kept once it has been read
        )
        self._audit.record(
            run.run_id,
            "llm.read",
            ACTOR,
            {
                "schema_id": result.findings.schema_id,
                "model": result.model,
                "attempts": result.attempts,
                "request_hash": result.request_hash,
                "response_hash": result.response_hash,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        )
        return result.findings

    def _materialize(self, run: RunRecord) -> None:
        assert run.plan is not None
        for planned in run.plan.steps:
            record = StepRecord(
                run_id=run.run_id,
                step_id=planned.id,
                tool=planned.tool,
                tool_version=self._registry.get(planned.tool).version,
                args_template=dict(planned.args),
                depends_on=planned.depends_on,
            )
            with contextlib.suppress(ValueError):  # already created before a restart
                self._steps.add(record)

    # -- running --------------------------------------------------------------------------

    def _execute(self, run: RunRecord) -> RunRecord:
        if run.status is RunStatus.PAUSED_FOR_APPROVAL:
            if not self._can_resume(run):
                return run
            run = self._runs.transition(
                run.run_id, expected=RunStatus.PAUSED_FOR_APPROVAL, to=RunStatus.RUNNING
            )
            self._audit.record(run.run_id, "run.resumed", ACTOR, {})
        assert run.plan is not None
        progress = True
        while progress:
            progress = False
            for planned in run.plan.steps:
                run = self._runs.get(run.run_id)
                step = self._steps.get(run.run_id, planned.id)
                if self._step_forward(run, step):
                    progress = True
        return self._settle(self._runs.get(run.run_id))

    def _can_resume(self, run: RunRecord) -> bool:
        assert run.plan is not None
        for planned in run.plan.steps:
            step = self._steps.get(run.run_id, planned.id)
            if step.status is StepStatus.AWAITING_APPROVAL:
                if self._approval_state(step) != "pending":
                    return True
            elif step.status in {StepStatus.APPROVED, *_IN_FLIGHT}:
                return True
        return False

    def _approval_state(self, step: StepRecord) -> str:
        assert step.action_hash is not None
        return self._approvals.state_for(
            run_id=step.run_id, step_id=step.step_id, action_hash=step.action_hash
        ).state

    def _step_forward(self, run: RunRecord, step: StepRecord) -> bool:
        """Advance one step by whatever its status calls for. True if anything changed."""
        status = step.status
        if status is StepStatus.PENDING:
            return self._start_step(run, step)
        if status is StepStatus.READY:
            self._evaluate_policy(run, step)
            return True
        if status is StepStatus.POLICY_CHECKED:
            decision = run.decisions.get(step.step_id)
            if decision is None:
                self._fail_step(run.run_id, step.step_id, "decision_missing")
                return True
            return self._after_policy(run, step, decision)
        if status is StepStatus.AWAITING_APPROVAL:
            return self._check_approval(run, step)
        if status in {StepStatus.APPROVED, *_IN_FLIGHT}:
            return self._run_tool(run, step)
        return False

    def _start_step(self, run: RunRecord, step: StepRecord) -> bool:
        states = [self._steps.get(run.run_id, dep).status for dep in step.depends_on]
        if any(state in _BAD_UPSTREAM for state in states):
            self._fail_step(run.run_id, step.step_id, "upstream_failed")
            return True
        if not all(state is StepStatus.SUCCEEDED for state in states):
            return False
        self._steps.transition(
            run.run_id, step.step_id, expected=StepStatus.PENDING, to=StepStatus.READY
        )
        self._evaluate_policy(run, self._steps.get(run.run_id, step.step_id))
        return True

    def _evaluate_policy(self, run: RunRecord, step: StepRecord) -> None:
        outputs = {dep: self._executions.load_output(run.run_id, dep) for dep in step.depends_on}
        try:
            resolved = resolve_args(step.args_template, outputs)
        except RefResolutionError:
            self._fail_step(run.run_id, step.step_id, "ref_unresolved")
            return
        manifest = self._registry.get(step.tool, step.tool_version)
        ctx = EvaluationContext(
            run_id=run.run_id,
            step_id=step.step_id,
            manifest=manifest,
            resolved_args=resolved,
            acting_user_id=run.acting_user_id,
            user_roles=frozenset(run.roles),
            variables=self._variables(run),
        )
        with self._telemetry.span("gawe.policy.evaluate", step_id=step.step_id, tool=step.tool):
            decision = self._policy.evaluate(ctx)
        run = self._runs.update(run.run_id, decisions={**run.decisions, step.step_id: decision})
        checked = self._steps.transition(
            run.run_id,
            step.step_id,
            expected=StepStatus.READY,
            to=StepStatus.POLICY_CHECKED,
            action_hash=decision.action_hash,
            resolved_args=resolved,
        )
        self._telemetry.count("gawe.policy.decision", tool=step.tool, outcome=decision.outcome)
        self._audit.record(
            run.run_id,
            "policy.decided",
            ACTOR,
            {
                "step_id": step.step_id,
                "outcome": decision.outcome,
                "rule_ids": list(decision.rule_ids),
                "action_hash": decision.action_hash,
                "approver_role": decision.approver_role,
            },
        )
        self._after_policy(run, checked, decision)

    def _after_policy(self, run: RunRecord, step: StepRecord, decision: PolicyDecision) -> bool:
        if decision.outcome == "deny":
            self._steps.transition(
                run.run_id, step.step_id, expected=StepStatus.POLICY_CHECKED, to=StepStatus.DENIED
            )
            self._audit.record(
                run.run_id,
                "step.denied",
                ACTOR,
                {"step_id": step.step_id, "rule_ids": list(decision.rule_ids)},
            )
            self._telemetry.count("gawe.step.outcome", tool=step.tool, status="denied")
            return True
        if decision.outcome == "needs_approval":
            assert step.action_hash is not None
            if self._approval_state(step) == "none":
                with contextlib.suppress(ApprovalAlreadyOpenError):  # asked just before a restart
                    self._approvals.request(
                        run_id=run.run_id, decision=decision, requested_by=run.acting_user_id
                    )
            self._steps.transition(
                run.run_id,
                step.step_id,
                expected=StepStatus.POLICY_CHECKED,
                to=StepStatus.AWAITING_APPROVAL,
            )
            return True
        return self._run_tool(run, step)

    def _check_approval(self, run: RunRecord, step: StepRecord) -> bool:
        assert step.action_hash is not None
        status = self._approvals.state_for(
            run_id=run.run_id, step_id=step.step_id, action_hash=step.action_hash
        )
        if status.state == "pending":
            return False
        if status.record is not None and status.record.decided_at is not None:
            latency = (status.record.decided_at - status.record.requested_at).total_seconds()
            self._telemetry.observe("gawe.approval.latency_seconds", latency, decision=status.state)
        if status.state == "approved":
            self._steps.transition(
                run.run_id,
                step.step_id,
                expected=StepStatus.AWAITING_APPROVAL,
                to=StepStatus.APPROVED,
            )
            self._audit.record(run.run_id, "step.approved", ACTOR, {"step_id": step.step_id})
            return self._run_tool(run, self._steps.get(run.run_id, step.step_id))
        if status.state == "rejected":
            self._steps.transition(
                run.run_id,
                step.step_id,
                expected=StepStatus.AWAITING_APPROVAL,
                to=StepStatus.REJECTED,
            )
            self._audit.record(run.run_id, "step.rejected", ACTOR, {"step_id": step.step_id})
            self._telemetry.count("gawe.step.outcome", tool=step.tool, status="rejected")
            return True
        code = "approval_expired" if status.state == "expired" else "approval_missing"
        self._fail_step(run.run_id, step.step_id, code)
        return True

    def _run_tool(self, run: RunRecord, step: StepRecord) -> bool:
        decision = run.decisions.get(step.step_id)
        if decision is None:
            self._fail_step(run.run_id, step.step_id, "decision_missing")
            return True
        with self._telemetry.span("gawe.step.execute", step_id=step.step_id, tool=step.tool):
            try:
                outcome = self._executor.execute(run.run_id, step.step_id, decision)
            except ConcurrentExecutionError:
                return False  # another worker owns the step right now
            except ExecutionRefusedError as refusal:
                self._fail_step(run.run_id, step.step_id, f"refused_{refusal}")
                return True
        verification_ran = outcome.status is StepStatus.SUCCEEDED or str(outcome.error).startswith(
            "verification_failed"
        )
        if verification_ran:
            self._telemetry.count("gawe.verification", ok=outcome.status is StepStatus.SUCCEEDED)
        self._telemetry.count("gawe.step.outcome", tool=step.tool, status=outcome.status.value)
        return True

    def _fail_step(self, run_id: str, step_id: str, code: str) -> None:
        for _ in range(3):
            step = self._steps.get(run_id, step_id)
            if step.status in STEP_TERMINAL:
                return
            try:
                self._steps.transition(
                    run_id, step_id, expected=step.status, to=StepStatus.FAILED, error=code
                )
            except StaleStateError:
                continue
            self._audit.record(run_id, "step.failed", ACTOR, {"step_id": step_id, "error": code})
            self._telemetry.count("gawe.step.outcome", tool=step.tool, status="failed")
            return

    # -- finishing ------------------------------------------------------------------------

    def _settle(self, run: RunRecord) -> RunRecord:
        assert run.plan is not None
        steps = [self._steps.get(run.run_id, s.id) for s in run.plan.steps]
        statuses = {s.status for s in steps}
        if statuses & _IN_FLIGHT:
            return run  # another worker is mid-execution; check back later
        if statuses & {StepStatus.DENIED, StepStatus.REJECTED}:
            self._terminate_remaining(run, steps)
            code = "policy_denied" if StepStatus.DENIED in statuses else "approval_rejected"
            return self._finish(run, RunStatus.DENIED, code)
        if StepStatus.FAILED in statuses:
            self._terminate_remaining(run, steps)
            return self._finish(run, RunStatus.FAILED, "step_failed")
        if statuses == {StepStatus.SUCCEEDED}:
            return self._finish(run, RunStatus.COMPLETED, None)
        awaiting = [s.step_id for s in steps if s.status is StepStatus.AWAITING_APPROVAL]
        if awaiting and run.status is RunStatus.RUNNING:
            run = self._runs.transition(
                run.run_id, expected=RunStatus.RUNNING, to=RunStatus.PAUSED_FOR_APPROVAL
            )
            self._audit.record(run.run_id, "run.paused", ACTOR, {"awaiting": awaiting})
        return run

    def _terminate_remaining(self, run: RunRecord, steps: list[StepRecord]) -> None:
        for step in steps:
            if step.status not in STEP_TERMINAL:
                self._fail_step(run.run_id, step.step_id, "run_terminated")

    def _finish(self, run: RunRecord, to: RunStatus, error: str | None) -> RunRecord:
        current = self._runs.get(run.run_id)
        finished = self._runs.transition(
            run.run_id,
            expected=current.status,
            to=to,
            error=error,
            completed_at=self._clock(),
            document=None,  # a finished run keeps no raw document
        )
        self._audit.record(run.run_id, f"run.{to.value}", ACTOR, {"error": error})
        self._approvals.close_for_run(run.run_id, reason=f"run_{to.value}")
        self._record_outcome(finished)
        self._notify_finished(finished)
        return finished

    def _notify_finished(self, run: RunRecord) -> None:
        if self._on_finished is None:
            return
        try:
            self._on_finished(run)
        except Exception:  # anchoring is evidence about the run, never a reason to fail it
            log.exception("on_finished hook failed for run %s", run.run_id)

    def _record_outcome(self, run: RunRecord) -> None:
        self._telemetry.count("gawe.run.outcome", status=run.status.value)
        if run.completed_at is not None:
            elapsed = (run.completed_at - run.created_at).total_seconds()
            self._telemetry.observe("gawe.run.duration_seconds", elapsed, status=run.status.value)

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(run_id, threading.Lock())
