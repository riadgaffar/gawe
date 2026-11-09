"""Everything the API does, with no web framework in sight.

Every method takes the caller's `Identity` and enforces, in this order: the tenant boundary
(another tenant's run or approval is reported as not found), then the caller's role or ownership.
The routes only translate HTTP to these calls, so the rules are tested here once.

Who may do what:
* start a run, list/view runs, view a run's audit trail: any authenticated user in the tenant;
* cancel or resume a run: the user who started it, or anyone with the `admin` role;
* see an approval in the inbox: someone holding the approver role the policy named, who did not
  request it themselves;
* decide an approval: the approval service's rules (hash match, expiry, separation of duties,
  role); this layer only adds the tenant check and maps failures to API errors.
"""

from dataclasses import dataclass

from gawe.api.auth import Identity
from gawe.api.background import Background
from gawe.api.errors import (
    ApiError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from gawe.api.schemas import (
    AnchorView,
    ApprovalView,
    AuditEventView,
    AuditVerificationView,
    EventsPage,
    RunView,
    StepView,
    ToolView,
)
from gawe.core.anchor import Anchorer
from gawe.core.approvals import (
    ActionHashMismatchError,
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalRecord,
    ApprovalService,
    ApprovalStore,
    ApproverNotAuthorizedError,
    Decision,
    Principal,
    SeparationOfDutiesError,
)
from gawe.core.audit import AnchorPoint, AuditLog
from gawe.core.models import AuditEvent
from gawe.core.records import StepNotFoundError, StepRecord, StepStore
from gawe.core.registry import ToolRegistry
from gawe.core.runs import RunNotFoundError, RunRecord, RunStore
from gawe.core.statemachine import RUN_TERMINAL, StepStatus
from gawe.engine.orchestrator import Orchestrator, RunBusyError
from gawe.engine.recovery import resume_interrupted
from gawe.llm.planner import MAX_OBJECTIVE_CHARS

ADMIN_ROLE = "admin"
AUDIT_ACTOR = "gawe.api"


@dataclass(frozen=True)
class Limits:
    max_document_chars: int = 200_000
    max_list: int = 100
    max_comment_chars: int = 2000


class ApiService:
    def __init__(
        self,
        *,
        orchestrator: Orchestrator,
        runs: RunStore,
        steps: StepStore,
        approval_service: ApprovalService,
        approval_store: ApprovalStore,
        registry: ToolRegistry,
        audit: AuditLog,
        background: Background,
        profiles: frozenset[str],
        limits: Limits | None = None,
        anchorer: Anchorer | None = None,
    ) -> None:
        self._orch = orchestrator
        self._runs = runs
        self._steps = steps
        self._approvals = approval_service
        self._approval_store = approval_store
        self._registry = registry
        self._audit = audit
        self._background = background
        self._profiles = profiles
        self._limits = limits or Limits()
        self._anchorer = anchorer or Anchorer(audit, [])

    # -- tools ----------------------------------------------------------------------------

    def list_tools(self, who: Identity) -> list[ToolView]:
        return [
            ToolView(
                name=m.name,
                version=m.version,
                description=m.description,
                side_effect_class=m.side_effect_class,
                risk_tier=m.risk_tier,
                required_roles=m.required_roles,
                input_schema=m.input_schema,
            )
            for m in sorted(self._registry.list(), key=lambda m: m.name)
        ]

    # -- runs -----------------------------------------------------------------------------

    def start_run(
        self, who: Identity, *, objective: str, profile: str, document: str | None = None
    ) -> RunView:
        if not objective.strip():
            raise BadRequestError("objective is required")
        if len(objective) > MAX_OBJECTIVE_CHARS:
            raise BadRequestError(f"objective is longer than {MAX_OBJECTIVE_CHARS} characters")
        if profile not in self._profiles:
            raise BadRequestError("unknown profile")
        if document is not None and len(document) > self._limits.max_document_chars:
            raise BadRequestError(
                f"document is longer than {self._limits.max_document_chars} characters"
            )
        try:
            run = self._orch.start_run(
                tenant_id=who.tenant_id,
                acting_user_id=who.user_id,
                roles=tuple(sorted(who.roles)),
                objective=objective,
                profile=profile,
                document=document,
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        self._kick(run.run_id)
        return self._run_view(run)

    def get_run(self, who: Identity, run_id: str) -> RunView:
        return self._run_view(self._own_run(who, run_id))

    def list_runs(self, who: Identity, *, limit: int = 50) -> list[RunView]:
        limit = max(1, min(limit, self._limits.max_list))
        return [self._run_view(r) for r in self._runs.list_for_tenant(who.tenant_id, limit=limit)]

    def cancel_run(self, who: Identity, run_id: str) -> RunView:
        run = self._own_run(who, run_id)
        self._require_owner_or_admin(who, run)
        try:
            return self._run_view(self._orch.cancel(run_id, by=who.user_id))
        except RunBusyError as exc:
            raise ConflictError(str(exc)) from exc

    def resume_run(self, who: Identity, run_id: str) -> RunView:
        """Ask for the run to be driven again (for example after a server restart)."""
        run = self._own_run(who, run_id)
        self._require_owner_or_admin(who, run)
        if run.status not in RUN_TERMINAL:
            self._kick(run_id)
        return self._run_view(run)

    # -- approvals ------------------------------------------------------------------------

    def inbox(self, who: Identity) -> list[ApprovalView]:
        """Open approvals this person could decide right now."""
        views: list[ApprovalView] = []
        for record in self._approval_store.open_approvals():
            if record.approver_role not in who.roles or record.requested_by == who.user_id:
                continue
            run = self._find_run(record.run_id)
            if run is None or run.tenant_id != who.tenant_id:
                continue
            step = self._find_step(record.run_id, record.step_id)
            if step is None or step.status is not StepStatus.AWAITING_APPROVAL:
                continue  # the run ended or moved on; the approval is stale
            views.append(self._approval_view(record, run, step))
        return views[: self._limits.max_list]

    def decide(
        self,
        who: Identity,
        approval_id: str,
        *,
        decision: Decision,
        action_hash: str,
        comment: str | None = None,
    ) -> ApprovalView:
        if comment is not None and len(comment) > self._limits.max_comment_chars:
            raise BadRequestError("comment is too long")
        try:
            record = self._approval_store.get(approval_id)
        except ApprovalNotFoundError:
            raise NotFoundError("approval not found") from None
        run = self._find_run(record.run_id)
        if run is None or run.tenant_id != who.tenant_id:
            raise NotFoundError("approval not found")
        try:
            decided = self._approvals.decide(
                approval_id,
                decider=Principal(who.user_id, who.roles),
                decision=decision,
                action_hash=action_hash,
                comment=comment,
            )
        except (SeparationOfDutiesError, ApproverNotAuthorizedError) as exc:
            raise ForbiddenError(str(exc)) from exc
        except ActionHashMismatchError as exc:
            raise ConflictError(str(exc)) from exc
        except (ApprovalExpiredError, ApprovalAlreadyDecidedError) as exc:
            raise ConflictError(str(exc)) from exc
        self._kick(decided.run_id)
        step = self._find_step(decided.run_id, decided.step_id)
        if step is None:
            raise NotFoundError("approval not found")
        return self._approval_view(decided, run, step)

    # -- audit ----------------------------------------------------------------------------

    def audit_events(self, who: Identity, run_id: str) -> list[AuditEventView]:
        self._own_run(who, run_id)
        return [_event_view(e) for e in self._audit.events(run_id)]

    def verify_audit(
        self, who: Identity, run_id: str, *, anchor: AnchorPoint | None = None
    ) -> AuditVerificationView:
        """Check the chain, and against `anchor` (the caller's own copy) or the stored anchor.

        A mismatched or invalid anchor makes `ok` false. Hashes the caller sent are never echoed.
        """
        run = self._own_run(who, run_id)
        try:
            check = self._anchorer.verify(run, caller=anchor)
        except ValueError:
            raise BadRequestError(
                "anchor must be a positive sequence number and a lowercase SHA-256 hash"
            ) from None
        chain = check.chain
        record, evidence = check.record, check.evidence
        if record is not None:
            seq: int | None = record.point.seq
        else:
            seq = anchor.seq if anchor is not None else None
        return AuditVerificationView(
            ok=check.ok,
            checked=chain.checked,
            head=chain.head,
            failed_seq=chain.failed_seq,
            reason=chain.reason,
            anchor=AnchorView(
                status=check.status,
                source=check.source,
                sink=record.sink if record is not None else None,
                external=evidence.external if evidence is not None else False,
                seq=seq,
                anchored_at=record.anchored_at if record is not None else None,
                timestamped_at=evidence.timestamped_at if evidence is not None else None,
                events_after_anchor=chain.events_after_anchor,
            ),
        )

    def events_after(
        self, who: Identity, run_id: str, *, after_seq: int = 0, limit: int = 200
    ) -> EventsPage:
        run = self._own_run(who, run_id)
        events = [e for e in self._audit.events(run_id) if e.seq > after_seq][:limit]
        finished = run.status in RUN_TERMINAL and len(events) < limit
        return EventsPage(
            events=tuple(_event_view(e) for e in events),
            run_status=run.status.value,
            finished=finished,
        )

    # -- internals ------------------------------------------------------------------------

    def _own_run(self, who: Identity, run_id: str) -> RunRecord:
        run = self._find_run(run_id)
        if run is None or run.tenant_id != who.tenant_id:
            raise NotFoundError("run not found")
        return run

    def _find_run(self, run_id: str) -> RunRecord | None:
        try:
            return self._runs.get(run_id)
        except RunNotFoundError:
            return None
        except ValueError:  # a malformed id cannot name a run
            return None

    def _find_step(self, run_id: str, step_id: str) -> StepRecord | None:
        try:
            return self._steps.get(run_id, step_id)
        except StepNotFoundError:
            return None

    @staticmethod
    def _require_owner_or_admin(who: Identity, run: RunRecord) -> None:
        if run.acting_user_id != who.user_id and ADMIN_ROLE not in who.roles:
            raise ForbiddenError("only the person who started the run, or an admin, can do this")

    def recover_interrupted(self) -> list[str]:
        """Drive every unfinished run again. Call once at startup; safe to call repeatedly."""
        return resume_interrupted(self._runs, self._kick)

    def _kick(self, run_id: str) -> None:
        self._background.submit(lambda: self._advance(run_id))

    def _advance(self, run_id: str) -> None:
        try:
            self._orch.advance(run_id)
        except ApiError:
            raise
        except Exception as exc:
            # Leave the run where it is (it can be resumed); record only the error class.
            self._audit.record(
                run_id, "run.advance_error", AUDIT_ACTOR, {"error": type(exc).__name__}
            )

    def _run_view(self, run: RunRecord) -> RunView:
        steps: list[StepView] = []
        for planned in run.plan.steps if run.plan else ():
            step = self._find_step(run.run_id, planned.id)
            if step is None:
                continue
            decision = run.decisions.get(step.step_id)
            steps.append(
                StepView(
                    step_id=step.step_id,
                    tool=step.tool,
                    status=step.status.value,
                    depends_on=step.depends_on,
                    policy_outcome=decision.outcome if decision else None,
                    policy_rule_ids=decision.rule_ids if decision else (),
                    approver_role=decision.approver_role if decision else None,
                    action_hash=step.action_hash,
                    error=step.error,
                )
            )
        return RunView(
            run_id=run.run_id,
            objective=run.objective,
            profile=run.profile,
            status=run.status.value,
            error=run.error,
            acting_user_id=run.acting_user_id,
            plan_version=run.plan_version,
            created_at=run.created_at,
            completed_at=run.completed_at,
            steps=tuple(steps),
        )

    def _approval_view(
        self, record: ApprovalRecord, run: RunRecord, step: StepRecord
    ) -> ApprovalView:
        decision = run.decisions.get(step.step_id)
        manifest = self._registry.get(step.tool, step.tool_version)
        return ApprovalView(
            approval_id=record.approval_id,
            run_id=record.run_id,
            step_id=record.step_id,
            tool=step.tool,
            risk_tier=manifest.risk_tier,
            resolved_args=step.resolved_args,
            action_hash=record.action_hash,
            approver_role=record.approver_role,
            reason=decision.reason if decision else "",
            requested_by=record.requested_by,
            requested_at=record.requested_at,
            expires_at=record.expires_at,
            decision=record.decision,
            decided_by=record.decided_by,
        )


def _event_view(event: AuditEvent) -> AuditEventView:
    return AuditEventView(
        seq=event.seq,
        type=event.type,
        actor=event.actor,
        payload_hash=event.payload_hash,
        hash=event.hash,
        ts=event.ts,
    )
