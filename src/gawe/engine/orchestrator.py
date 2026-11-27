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

`on_finished` is called when a run ends and after new post-terminal verification evidence
is appended. It follows the last audit event (after open
approvals are closed), which is where the audit head gets anchored. It can never fail the run.
"""

import contextlib
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator

from gawe.core.approvals import ApprovalAlreadyOpenError, ApprovalService
from gawe.core.audit import AuditLog
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.executor import ConcurrentExecutionError, ExecutionRefusedError, Executor
from gawe.core.history import HISTORY_VARIABLE, History, HistoryRead
from gawe.core.maintenance import run_target
from gawe.core.models import REF_KEY, PolicyDecision, is_ref
from gawe.core.policy.audit_payload import decided_payload
from gawe.core.policy.engine import PolicyEngine
from gawe.core.policy.rules import EvaluationContext
from gawe.core.preparation import ActionVariables, PreparationError, Preparer
from gawe.core.records import ExecutionStore, Handoff, StaleStateError, StepRecord, StepStore
from gawe.core.refs import RefResolutionError, resolve_args
from gawe.core.registry import RegistryError, ToolRegistry
from gawe.core.runs import RunRecord, RunStore, StaleRunError
from gawe.core.statemachine import RUN_TERMINAL, STEP_TERMINAL, RunStatus, StepStatus
from gawe.core.telemetry import NoopTelemetry, Telemetry
from gawe.core.undo import legacy_restore_version
from gawe.core.verification import READBACK_KIND
from gawe.llm.planner import Planner, PlanningError
from gawe.llm.provider import LLMUnavailableError
from gawe.llm.reader import Findings, Reader, ReaderError

log = logging.getLogger("gawe.engine")

ACTOR = "gawe.orchestrator"
_RUN_TERMINAL = RUN_TERMINAL
_IN_FLIGHT = frozenset({StepStatus.EXECUTING, StepStatus.VERIFYING})
_UNDISPATCHED = frozenset(
    {
        StepStatus.PENDING,
        StepStatus.READY,
        StepStatus.POLICY_CHECKED,
        StepStatus.AWAITING_APPROVAL,
        StepStatus.APPROVED,
    }
)
_BAD_UPSTREAM = frozenset(
    {
        StepStatus.FAILED,
        StepStatus.DENIED,
        StepStatus.REJECTED,
        StepStatus.VERIFICATION_BLOCKED,
        StepStatus.VERIFICATION_UNKNOWN,
        StepStatus.NOT_DONE_BY_PERSON,
    }
)
# What lets a dependant start: the engine's own success, or a person saying they did it (M20).
_DONE = frozenset({StepStatus.SUCCEEDED, StepStatus.DONE_BY_PERSON})


class HandoffError(Exception):
    """A hand-off mark was refused; the message is a short code."""


class RunBusyError(Exception):
    """The run cannot be changed right now (a step is mid-execution)."""


class PlanningUnavailableError(Exception):
    """This orchestrator was built without a planner (maintenance-only process)."""


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
        planner: Planner | None,
        audit: AuditLog,
        reader: Reader | None = None,
        telemetry: Telemetry | None = None,
        variables: Callable[[RunRecord], Mapping[str, Any]] | None = None,
        preparers: Mapping[tuple[str, str], Preparer] | None = None,
        action_variables: Mapping[tuple[str, str], ActionVariables] | None = None,
        on_finished: Callable[[RunRecord], object] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        prepare_only: Mapping[tuple[str, str], str] | None = None,
        history: History | None = None,
    ) -> None:
        self._history = history
        self._prepare_only = dict(prepare_only or {})
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
        self._preparers = dict(preparers or {})
        self._action_variables = dict(action_variables or {})
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
        source_id: str | None = None,
    ) -> RunRecord:
        """`source_id` names the record the document was fetched from; the caller (the API
        service) fetched it, so a run never holds text and a source that disagree."""
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
            source_id=source_id,
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
                # Only runs with a source carry the key, so earlier payloads keep their shape.
                **({"source_id": source_id} if source_id is not None else {}),
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
                reconciled = False
                if run.status not in {RunStatus.CREATED, RunStatus.PLANNING}:
                    # Also finish a result whose worker died before step/audit/run bookkeeping.
                    reconciled = self._reconcile_pending(run)
                if run.status in {RunStatus.WAITING_VERIFICATION, *_RUN_TERMINAL}:
                    if run.status in _RUN_TERMINAL:
                        if reconciled:
                            self._notify_finished(run)
                        if self._executor.verification:
                            self._executor.verification.acknowledge(run_id)
                        return run
                    run = self._settle(run)
                    if run.status is RunStatus.WAITING_VERIFICATION:
                        if self._executor.verification:
                            self._executor.verification.acknowledge(run_id)
                        return run
                if run.status in (RunStatus.CREATED, RunStatus.PLANNING):
                    run = self._plan(run)
                if run.status in (
                    RunStatus.RUNNING,
                    RunStatus.PAUSED_FOR_APPROVAL,
                    RunStatus.WAITING_FOR_PERSON,
                ):
                    run = self._execute(run)
                span.set("status", run.status.value)
                if self._executor.verification:
                    self._executor.verification.acknowledge(run_id)
                return run
        except StaleRunError:
            return self._runs.get(run_id)  # another driver settled this run first
        finally:
            lock.release()

    def cancel(self, run_id: str, *, by: str) -> RunRecord:
        with self._lock_for(run_id):
            run = self._runs.get(run_id)
            if run.status in _RUN_TERMINAL:
                return run
            if run.profile == "undo" and run.plan is not None:
                # Trusted undo plans exist before their first advance; cancellation still needs
                # step records to terminate. Materialization is idempotent and performs no IO.
                self._materialize(run)
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

    def _reconcile_pending(self, run: RunRecord) -> bool:
        resolver = self._executor.verification
        if resolver is None:
            return False
        reconciled = False
        for job in resolver.jobs.for_run(run.tenant_id, run.run_id):
            if job.kind != READBACK_KIND or job.continued or job.plan_version != run.plan_version:
                continue
            step = self._steps.get(run.run_id, run_target(job).step_id)
            if step.action_hash != job.action_hash:
                continue
            try:
                self._executor.reconcile(step)
                reconciled = True
            except ConcurrentExecutionError:
                continue
        return reconciled

    # -- planning -------------------------------------------------------------------------

    def _plan(self, run: RunRecord) -> RunRecord:
        if run.status is RunStatus.CREATED:
            run = self._runs.transition(
                run.run_id, expected=RunStatus.CREATED, to=RunStatus.PLANNING
            )
            self._audit.record(run.run_id, "run.planning", ACTOR, {})
        if run.plan is None:
            if run.profile == "undo":
                return run  # durable undo-request recovery supplies the trusted plan
            if self._planner is None:
                # A maintenance-only driver never plans; the run stays where it is.
                raise PlanningUnavailableError("this process does not plan runs")
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
            version = planned.tool_version
            if run.profile == "undo":
                version = version or legacy_restore_version(planned.tool)
            elif version is not None:
                raise ValueError("tool version pins require a trusted undo plan")
            manifest = self._registry.get(planned.tool, version)
            if run.profile == "undo" and not manifest.trusted_only:
                raise ValueError("an undo plan must use a trusted-only tool")
            record = StepRecord(
                run_id=run.run_id,
                step_id=planned.id,
                tool=planned.tool,
                tool_version=manifest.version,
                args_template=dict(planned.args),
                depends_on=planned.depends_on,
            )
            with contextlib.suppress(ValueError):  # already created before a restart
                self._steps.add(record)

    # -- running --------------------------------------------------------------------------

    def _execute(self, run: RunRecord) -> RunRecord:
        if run.status is RunStatus.WAITING_FOR_PERSON:
            if self._still_prepared(run):
                return run  # nothing to do until the person marks it
            run = self._runs.transition(
                run.run_id, expected=RunStatus.WAITING_FOR_PERSON, to=RunStatus.RUNNING
            )
            self._audit.record(run.run_id, "run.resumed", ACTOR, {})
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
                try:
                    if self._step_forward(run, step):
                        progress = True
                except StaleStateError:
                    return self._runs.get(run.run_id)
                if self._steps.get(run.run_id, planned.id).status is StepStatus.AWAITING_READBACK:
                    return self._settle(self._runs.get(run.run_id))
        return self._settle(self._runs.get(run.run_id))

    def _still_prepared(self, run: RunRecord) -> bool:
        assert run.plan is not None
        return any(
            self._steps.get(run.run_id, s.id).status is StepStatus.PREPARED for s in run.plan.steps
        )

    def _can_resume(self, run: RunRecord) -> bool:
        assert run.plan is not None
        for planned in run.plan.steps:
            step = self._steps.get(run.run_id, planned.id)
            if (
                step.tool == "email.send"
                and step.tool_version == "1.0.0"
                and step.status not in STEP_TERMINAL
            ):
                return True
            if step.status in _UNDISPATCHED and self._retired(step):
                return True
            if step.status is StepStatus.AWAITING_APPROVAL:
                if self._approval_state(step) != "pending":
                    return True
            elif step.status in {
                StepStatus.READY,
                StepStatus.POLICY_CHECKED,
                StepStatus.APPROVED,
                *_IN_FLIGHT,
            }:
                return True
        return False

    def _retired(self, step: StepRecord) -> bool:
        try:
            return self._registry.get(step.tool, step.tool_version).retired
        except RegistryError:
            return False

    def _approval_state(self, step: StepRecord) -> str:
        assert step.action_hash is not None
        return self._approvals.state_for(
            run_id=step.run_id, step_id=step.step_id, action_hash=step.action_hash
        ).state

    def _step_forward(self, run: RunRecord, step: StepRecord) -> bool:
        """Advance one step by whatever its status calls for. True if anything changed."""
        status = step.status
        if status not in STEP_TERMINAL:
            try:
                manifest = self._registry.get(step.tool, step.tool_version)
            except RegistryError:
                code = "tool_not_registered"
                if step.tool == "email.send" and step.tool_version == "1.0.0":
                    code = (
                        "legacy_send_reconciliation_required"
                        if status in _IN_FLIGHT
                        else "unsupported_email_version"
                    )
                self._fail_step(run.run_id, step.step_id, code)
                return True
            if manifest.retired and status in _UNDISPATCHED:
                # Registered only to verify earlier dispatches; never relabel or resend.
                self._fail_step(run.run_id, step.step_id, "unsupported_tool_version")
                return True
        if status is StepStatus.PENDING:
            return self._start_step(run, step)
        if status is StepStatus.READY:
            self._evaluate_policy(run, step)
            return True
        if status is StepStatus.POLICY_CHECKED:
            decision = step.policy_decision or run.decisions.get(step.step_id)
            if decision is None:
                self._fail_step(run.run_id, step.step_id, "decision_missing")
                return True
            return self._after_policy(run, step, decision)
        if status is StepStatus.AWAITING_APPROVAL:
            return self._check_approval(run, step)
        if status in {StepStatus.APPROVED, StepStatus.AWAITING_READBACK, *_IN_FLIGHT}:
            return self._run_tool(run, step)
        return False

    def _start_step(self, run: RunRecord, step: StepRecord) -> bool:
        states = [self._steps.get(run.run_id, dep).status for dep in step.depends_on]
        if any(state in _BAD_UPSTREAM for state in states):
            self._fail_step(run.run_id, step.step_id, "upstream_failed")
            return True
        if not all(state in _DONE for state in states):
            return False
        self._steps.transition(
            run.run_id, step.step_id, expected=StepStatus.PENDING, to=StepStatus.READY
        )
        self._evaluate_policy(run, self._steps.get(run.run_id, step.step_id))
        return True

    def _evaluate_policy(self, run: RunRecord, step: StepRecord) -> None:
        for value in step.args_template.values():
            if is_ref(value):
                source = str(value[REF_KEY]).partition(".output")[0]
                if self._steps.get(run.run_id, source).status is StepStatus.DONE_BY_PERSON:
                    # A person's action has no output the engine can trust.
                    self._fail_step(run.run_id, step.step_id, "handoff_has_no_output")
                    return
        outputs = {dep: self._executions.load_output(run.run_id, dep) for dep in step.depends_on}
        try:
            resolved = resolve_args(step.args_template, outputs)
        except RefResolutionError:
            self._fail_step(run.run_id, step.step_id, "ref_unresolved")
            return
        manifest = self._registry.get(step.tool, step.tool_version)
        metadata = dict(step.preparation)
        if manifest.preparation_required:
            if any(
                Draft202012Validator(manifest.proposal_schema or manifest.input_schema).iter_errors(
                    resolved
                )
            ):
                self._fail_step(run.run_id, step.step_id, "preparation_invalid_proposal")
                return
            try:
                prepared = self._preparers[(step.tool, step.tool_version)](
                    resolved, reapproval=bool(metadata.get("reapproval_required")), run=run
                )
            except PreparationError as exc:
                self._fail_step(run.run_id, step.step_id, str(exc))
                return
            resolved = dict(prepared.args)
            metadata = dict(prepared.metadata)
        subject = self._history.subject(step.tool, resolved) if self._history else None
        hold = (
            self._history.hold(run.tenant_id, step.tool, subject)
            if self._history is not None and subject is not None
            else contextlib.nullcontext()
        )
        # M24: reading history, evaluating and storing the decision happen under one lock per
        # subject, so a second run judging the same subject sees this step in flight.
        with hold:
            action_variables = self._action_variables.get((step.tool, step.tool_version))
            try:
                variables = dict(
                    action_variables(run, resolved) if action_variables else self._variables(run)
                )
            except PreparationError as exc:
                self._fail_step(run.run_id, step.step_id, str(exc))
                return
            read = self._read_history(run, step, resolved)
            if read is not None:
                variables[HISTORY_VARIABLE] = read.variables
                metadata = {**metadata, "history": list(read.shown)}
            ctx = EvaluationContext(
                run_id=run.run_id,
                step_id=step.step_id,
                manifest=manifest,
                resolved_args=resolved,
                acting_user_id=run.acting_user_id,
                user_roles=frozenset(run.roles),
                variables=variables,
            )
            with self._telemetry.span("gawe.policy.evaluate", step_id=step.step_id, tool=step.tool):
                decision = self._policy.evaluate(ctx)
            checked = self._steps.transition(
                run.run_id,
                step.step_id,
                expected=StepStatus.READY,
                to=StepStatus.POLICY_CHECKED,
                action_hash=decision.action_hash,
                resolved_args=resolved,
                expected_action_hash=step.action_hash,
                preparation=metadata,
                policy_decision=decision,
            )
        run = self._runs.update(run.run_id, decisions={**run.decisions, step.step_id: decision})
        self._telemetry.count("gawe.policy.decision", tool=step.tool, outcome=decision.outcome)
        self._audit.record(
            run.run_id,
            "policy.decided",
            ACTOR,
            decided_payload(decision),
        )
        self._after_policy(run, checked, decision)

    def _read_history(
        self, run: RunRecord, step: StepRecord, resolved: Mapping[str, Any]
    ) -> HistoryRead | None:
        """The step's history, audited by reference; None when the tool declares none or it
        can't be read, which leaves the history variables out so the step denies."""
        if self._history is None or not self._history.declared(step.tool):
            return None
        try:
            read = self._history.read(run, step.step_id, step.tool, resolved)
        except Exception as exc:  # noqa: BLE001 - unreadable history never relaxes a decision
            self._audit.record(
                run.run_id,
                "history.read",
                ACTOR,
                {"step_id": step.step_id, "tool": step.tool, "error": type(exc).__name__},
            )
            return None
        self._audit.record(
            run.run_id, "history.read", ACTOR, read.audit_payload(step.step_id, step.tool)
        )
        return read

    def _after_policy(self, run: RunRecord, step: StepRecord, decision: PolicyDecision) -> bool:
        if decision.outcome == "deny":
            self._steps.transition(
                run.run_id,
                step.step_id,
                expected=StepStatus.POLICY_CHECKED,
                to=StepStatus.DENIED,
                expected_action_hash=step.action_hash,
            )
            self._audit.record(
                run.run_id,
                "step.denied",
                ACTOR,
                {"step_id": step.step_id, "rule_ids": list(decision.rule_ids)},
            )
            self._telemetry.count("gawe.step.outcome", tool=step.tool, status="denied")
            return True
        role = self._prepare_only.get((step.tool, step.tool_version))
        if role is not None:
            return self._hand_off(run, step, decision, role)
        if decision.outcome == "needs_approval":
            assert step.action_hash is not None
            if self._approval_state(step) == "none":
                if step.preparation.get("reapproval_required"):
                    self._approvals.close_stale_for_step(run.run_id, step.step_id, step.action_hash)
                with contextlib.suppress(ApprovalAlreadyOpenError):  # asked just before a restart
                    self._approvals.request(
                        run_id=run.run_id,
                        decision=decision,
                        requested_by=run.acting_user_id,
                        preparation=step.preparation,
                        resolved_args=step.resolved_args,
                    )
            self._steps.transition(
                run.run_id,
                step.step_id,
                expected=StepStatus.POLICY_CHECKED,
                to=StepStatus.AWAITING_APPROVAL,
                expected_action_hash=step.action_hash,
            )
            return True
        return self._run_tool(run, step)

    def _hand_off(
        self, run: RunRecord, step: StepRecord, decision: PolicyDecision, role: str
    ) -> bool:
        """M20: the action is prepared, judged by policy (a deny already returned) and bound into
        its hash, and now waits for a person. It asks no approval and never reaches the executor."""
        self._steps.transition(
            run.run_id,
            step.step_id,
            expected=StepStatus.POLICY_CHECKED,
            to=StepStatus.PREPARED,
            expected_action_hash=step.action_hash,
            handoff=Handoff(role=role),
        )
        self._audit.record(
            run.run_id,
            "handoff.prepared",
            ACTOR,
            {
                "step_id": step.step_id,
                "action_hash": step.action_hash,
                "role": role,
                "policy_outcome": decision.outcome,
            },
        )
        return True

    def mark_handoff(
        self,
        run_id: str,
        step_id: str,
        *,
        user_id: str,
        roles: frozenset[str],
        done: bool,
        action_hash: str,
        reference: str | None = None,
    ) -> StepRecord:
        """A person says whether they carried out a prepared action. Refused (HandoffError, a
        short code) unless they hold its role, aren't the requester, name the current hash and the
        step is still waiting. Once marked, it can't be marked again."""
        with self._lock_for(run_id):
            run = self._runs.get(run_id)
            step = self._steps.get(run_id, step_id)
            if step.status is not StepStatus.PREPARED or step.handoff is None:
                raise HandoffError("not_waiting_for_a_person")
            if step.action_hash != action_hash:
                raise HandoffError("action_hash_mismatch")
            if step.handoff.role not in roles:
                raise HandoffError("role_required")
            if user_id == run.acting_user_id:
                raise HandoffError("requester_cannot_mark")
            try:
                handoff = Handoff(
                    role=step.handoff.role, by=user_id, at=self._clock(), reference=reference
                )
            except ValueError:
                raise HandoffError("reference_invalid") from None
            marked = self._steps.transition(
                run_id,
                step_id,
                expected=StepStatus.PREPARED,
                to=StepStatus.DONE_BY_PERSON if done else StepStatus.NOT_DONE_BY_PERSON,
                expected_action_hash=action_hash,
                handoff=handoff,
            )
            self._audit.record(
                run_id,
                "handoff.done" if done else "handoff.not_done",
                user_id,
                {
                    "step_id": step_id,
                    "action_hash": action_hash,
                    "role": handoff.role,
                    "reference_chars": len(reference or ""),
                },
            )
        return marked

    def _check_approval(self, run: RunRecord, step: StepRecord) -> bool:
        assert step.action_hash is not None
        status = self._approvals.state_for(
            run_id=run.run_id, step_id=step.step_id, action_hash=step.action_hash
        )
        if status.state == "pending":
            return False
        if status.state == "none" and step.preparation.get("reapproval_required"):
            # A delayed worker may have inserted an obsolete pending request between stores.
            decision = step.policy_decision
            if decision is not None and decision.outcome == "needs_approval":
                self._approvals.close_stale_for_step(run.run_id, step.step_id, step.action_hash)
                try:
                    self._approvals.request(
                        run_id=run.run_id,
                        decision=decision,
                        requested_by=run.acting_user_id,
                        preparation=step.preparation,
                        resolved_args=step.resolved_args,
                    )
                except ApprovalAlreadyOpenError:
                    return False
                return True
        if status.record is not None and status.record.decided_at is not None:
            latency = (status.record.decided_at - status.record.requested_at).total_seconds()
            self._telemetry.observe("gawe.approval.latency_seconds", latency, decision=status.state)
        if status.state == "approved":
            self._steps.transition(
                run.run_id,
                step.step_id,
                expected=StepStatus.AWAITING_APPROVAL,
                to=StepStatus.APPROVED,
                expected_action_hash=step.action_hash,
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
        decision = step.policy_decision or run.decisions.get(step.step_id)
        if decision is None:
            self._fail_step(run.run_id, step.step_id, "decision_missing")
            return True
        with self._telemetry.span("gawe.step.execute", step_id=step.step_id, tool=step.tool):
            try:
                outcome = self._executor.execute(run.run_id, step.step_id, decision)
            except ConcurrentExecutionError:
                return False  # another worker owns the step right now
            except ExecutionRefusedError as refusal:
                if self._steps.get(run.run_id, step.step_id).action_hash != step.action_hash:
                    return False
                self._fail_step(run.run_id, step.step_id, f"refused_{refusal}")
                return True
        verification_ran = outcome.status is StepStatus.SUCCEEDED or str(outcome.error).startswith(
            "verification_failed"
        )
        if verification_ran:
            self._telemetry.count("gawe.verification", ok=outcome.status is StepStatus.SUCCEEDED)
        self._telemetry.count("gawe.step.outcome", tool=step.tool, status=outcome.status.value)
        return outcome.status != step.status

    def _fail_step(self, run_id: str, step_id: str, code: str) -> None:
        for _ in range(3):
            step = self._steps.get(run_id, step_id)
            if step.status in {*STEP_TERMINAL, StepStatus.AWAITING_READBACK}:
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
        run = self._runs.get(run.run_id)
        if run.status in _RUN_TERMINAL:
            return run
        assert run.plan is not None
        steps = [self._steps.get(run.run_id, s.id) for s in run.plan.steps]
        statuses = {s.status for s in steps}
        if statuses & _IN_FLIGHT:
            return run  # another worker is mid-execution; check back later
        if statuses & {StepStatus.DENIED, StepStatus.REJECTED, StepStatus.NOT_DONE_BY_PERSON}:
            self._terminate_remaining(run, steps)
            if StepStatus.DENIED in statuses:
                code = "policy_denied"
            elif StepStatus.REJECTED in statuses:
                code = "approval_rejected"
            else:
                code = "handoff_not_done"  # a person chose not to do it, like a rejection
            return self._finish(run, RunStatus.DENIED, code)
        if StepStatus.VERIFICATION_UNKNOWN in statuses:
            self._terminate_remaining(run, steps)
            return self._finish(run, RunStatus.UNKNOWN, "verification_unknown")
        if statuses & {StepStatus.FAILED, StepStatus.VERIFICATION_BLOCKED}:
            self._terminate_remaining(run, steps)
            return self._finish(run, RunStatus.FAILED, "step_failed")
        if statuses <= _DONE:
            return self._finish(run, RunStatus.COMPLETED, None)
        if StepStatus.AWAITING_READBACK in statuses:
            if run.status is not RunStatus.WAITING_VERIFICATION:
                run = self._runs.transition(
                    run.run_id, expected=run.status, to=RunStatus.WAITING_VERIFICATION
                )
                self._audit.record(run.run_id, "run.waiting_verification", ACTOR, {})
            return run
        if run.status is RunStatus.WAITING_VERIFICATION:
            run = self._runs.transition(run.run_id, expected=run.status, to=RunStatus.RUNNING)
        awaiting = [s.step_id for s in steps if s.status is StepStatus.AWAITING_APPROVAL]
        if awaiting and run.status is RunStatus.RUNNING:
            run = self._runs.transition(
                run.run_id, expected=RunStatus.RUNNING, to=RunStatus.PAUSED_FOR_APPROVAL
            )
            self._audit.record(run.run_id, "run.paused", ACTOR, {"awaiting": awaiting})
            return run
        prepared = [s.step_id for s in steps if s.status is StepStatus.PREPARED]
        if prepared and run.status is RunStatus.RUNNING:
            run = self._runs.transition(
                run.run_id, expected=RunStatus.RUNNING, to=RunStatus.WAITING_FOR_PERSON
            )
            self._audit.record(run.run_id, "run.waiting_for_person", ACTOR, {"prepared": prepared})
        return run

    def _terminate_remaining(self, run: RunRecord, steps: list[StepRecord]) -> None:
        for step in steps:
            if step.status not in STEP_TERMINAL:
                self._fail_step(run.run_id, step.step_id, "run_terminated")

    def _finish(self, run: RunRecord, to: RunStatus, error: str | None) -> RunRecord:
        current = self._runs.get(run.run_id)
        if current.status in _RUN_TERMINAL:
            return current
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
