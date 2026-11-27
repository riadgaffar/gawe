"""Everything the API does, with no web framework in sight.

Every method takes the caller's `Identity` and enforces, in this order: the tenant boundary
(another tenant's run or approval is reported as not found), then the caller's role or ownership.
The routes only translate HTTP to these calls, so the rules are tested here once.

Who may do what:
* start a run, list/view runs, view a run's audit trail or a step's trace: any authenticated
  user in the tenant;
* cancel or resume a run: the user who started it, or anyone with the `admin` role;
* see an approval in the inbox: someone holding the approver role the policy named, who did not
  request it themselves;
* list next-day review items and give a verdict: the `legal_reviewer` role, and never on a run
  the reviewer started;
* decide an approval: the approval service's rules (hash match, expiry, separation of duties,
  role); this layer only adds the tenant check and maps failures to API errors;
* see and mark a hand-off (a prepare-only step waiting for a person): someone holding the role
  the setting names, who did not start the run; the orchestrator enforces it again on marking.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gawe.api import trace as step_trace
from gawe.api.auth import Identity
from gawe.api.background import Background
from gawe.api.errors import (
    ApiError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from gawe.api.record_evidence import RecordPresenter
from gawe.api.schemas import (
    AnchorView,
    ApprovalView,
    AuditEventView,
    AuditVerificationView,
    ChangeView,
    CheckView,
    DifferenceView,
    EventsPage,
    FreeTextFieldView,
    HandoffMarkView,
    HandoffView,
    HistoryItemView,
    ReviewItemView,
    RunView,
    StepTraceView,
    StepView,
    ToolView,
    TypedFieldView,
    VerificationView,
)
from gawe.api.undo import UndoRefused, UndoRuns
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
from gawe.core.fields import FREE_TEXT_LABEL, difference, fields, length
from gawe.core.maintenance import MaintenanceStore, run_target
from gawe.core.models import AuditEvent
from gawe.core.policy.signing import PolicySigner
from gawe.core.records import (
    ExecutionStore,
    StaleStateError,
    StepNotFoundError,
    StepRecord,
    StepStore,
)
from gawe.core.registry import ToolNotFoundError, ToolRegistry
from gawe.core.review import (
    InMemoryReviewStore,
    ReviewAlreadyDecidedError,
    ReviewItem,
    ReviewItemNotFoundError,
    ReviewStore,
    Status,
    Verdict,
)
from gawe.core.runs import MODEL_MODES, SOURCE_ID_PATTERN, RunNotFoundError, RunRecord, RunStore
from gawe.core.sources import DocumentSource, SourceError
from gawe.core.statemachine import RUN_TERMINAL, RunStatus, StepStatus
from gawe.core.verification import READBACK_KIND
from gawe.engine.orchestrator import HandoffError, Orchestrator, RunBusyError
from gawe.engine.recovery import resume_interrupted
from gawe.llm.planner import MAX_OBJECTIVE_CHARS
from gawe.maintenance.runner import MaintenanceRunner

ADMIN_ROLE = "admin"
AUDIT_ACTOR = "gawe.api"


REVIEWER_ROLE = "legal_reviewer"
_HANDOFF_SCAN_LIMIT = 500  # waiting runs read per listing, across tenants, oldest first


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        undo: UndoRuns | None = None,
        record_presenter: RecordPresenter | None = None,
        limits: Limits | None = None,
        anchorer: Anchorer | None = None,
        maintenance: MaintenanceStore | None = None,
        maintenance_runner: MaintenanceRunner | None = None,
        reviews: ReviewStore | None = None,
        sources: Mapping[str, DocumentSource] | None = None,
        checked_not_enforced: Mapping[tuple[str, str], tuple[str, ...]] | None = None,
        executions: ExecutionStore | None = None,
        signer: PolicySigner | None = None,
        readback_tools: frozenset[tuple[str, str]] = frozenset(),
        clock: Callable[[], datetime] = _utc_now,
        model_modes: bool = False,
    ) -> None:
        # M29: whether runs may choose a scripted demo model mode (only under the demo model).
        self._model_modes = model_modes
        self._executions = executions
        self._signer = signer
        self._readback_tools = readback_tools
        self._checked_not_enforced = dict(checked_not_enforced or {})
        self._orch = orchestrator
        self._runs = runs
        self._steps = steps
        self._approvals = approval_service
        self._approval_store = approval_store
        self._registry = registry
        self._audit = audit
        self._background = background
        self._profiles = profiles
        self._undo = undo
        self._record_presenter = record_presenter
        self._limits = limits or Limits()
        self._anchorer = anchorer or Anchorer(audit, [])
        self._maintenance = maintenance
        self._reviews = reviews or InMemoryReviewStore()
        self._sources = dict(sources or {})
        self._clock = clock
        self.maintenance_runner = maintenance_runner

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
                input_schema=m.proposal_schema or m.input_schema,
            )
            for m in sorted(self._registry.list(), key=lambda m: m.name)
        ]

    # -- runs -----------------------------------------------------------------------------

    def start_run(
        self,
        who: Identity,
        *,
        objective: str,
        profile: str,
        document: str | None = None,
        source_id: str | None = None,
        model_mode: str | None = None,
    ) -> RunView:
        """A profile with a source (IT access) takes `source_id`, never an uploaded document:
        the server fetches the text from that record in the caller's tenant. `model_mode`
        (M29) is accepted only when the API runs the scripted demo model."""
        if model_mode is not None:
            if not self._model_modes:
                raise BadRequestError("model_mode is only accepted under the scripted demo model")
            if model_mode not in MODEL_MODES:
                raise BadRequestError("model_mode must be honest or fooled")
        if not objective.strip():
            raise BadRequestError("objective is required")
        if len(objective) > MAX_OBJECTIVE_CHARS:
            raise BadRequestError(f"objective is longer than {MAX_OBJECTIVE_CHARS} characters")
        if profile == "undo" or profile not in self._profiles:
            raise BadRequestError("unknown profile")
        source = self._sources.get(profile)
        if source is None and source_id is not None:
            raise BadRequestError("this profile does not read from a source record")
        if source is not None:
            if document is not None:
                raise BadRequestError("this profile reads its document from its source record")
            if source_id is None:
                raise BadRequestError("source_id is required for this profile")
            if re.fullmatch(SOURCE_ID_PATTERN, source_id) is None:
                raise BadRequestError("source_id is malformed")
            try:
                document = source.fetch(who.tenant_id, source_id)
            except SourceError as exc:
                raise BadRequestError(f"source record unavailable: {exc.code}") from None
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
                source_id=source_id,
                model_mode=model_mode,  # checked above
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        self.kick(run.run_id)
        return self._run_view(run)

    def request_undo(self, who: Identity, run_id: str, step_id: str) -> RunView:
        run = self._own_run(who, run_id)
        step = self._find_step(run_id, step_id)
        if step is None:
            raise NotFoundError("step not found")
        if self._undo is None:
            raise UndoRefused("not_reversible")
        undone = self._undo.request(who, run, step)
        self.kick(undone.run_id)
        return self._run_view(self._runs.get(undone.run_id))

    def undo_view(self, who: Identity, run_id: str, step_id: str) -> dict[str, Any]:
        run = self._own_run(who, run_id)
        if self._find_step(run_id, step_id) is None:
            raise NotFoundError("step not found")
        records = [] if self._undo is None else self._undo.r.undo.for_run(who.tenant_id, run_id)
        record = next((r for r in records if r.step_id == step_id), None)
        later = []
        after = False
        for item in run.plan.steps if run.plan else ():
            if item.id == step_id:
                after = True
                continue
            if after and any(
                isinstance(v, dict)
                and isinstance(v.get("$ref"), str)
                and v["$ref"].split(".output")[0] == step_id
                for v in item.args.values()
            ):
                step = self._find_step(run_id, item.id)
                later.append(
                    {
                        "step_id": item.id,
                        "tool": item.tool,
                        "outcome": step.status.value if step else "pending",
                    }
                )
        return {
            "record": record.model_dump(mode="json") if record else None,
            "later_steps": later,
            "notice": "Effects of later steps stay as they are.",
        }

    def get_run(self, who: Identity, run_id: str) -> RunView:
        return self._run_view(self._own_run(who, run_id))

    def verification_evidence(self, who: Identity, run_id: str) -> list[VerificationView]:
        run = self._own_run(who, run_id)
        return self._verification_views(run)

    def _verification_views(self, run: RunRecord) -> list[VerificationView]:
        if self._maintenance is None:
            return []
        views = [
            VerificationView(
                job_id=j.job_id,
                step_id=run_target(j).step_id,
                plan_version=run_target(j).plan_version,
                operation_key=j.operation_key,
                action_hash=run_target(j).action_hash,
                deadline=j.deadline,
                due_at=j.due_at,
                outcome=j.result.get("outcome") if j.result else None,
                code=j.result.get("code") if j.result else j.evidence.get("code"),
                protocol_error=j.protocol_error,
                evidence=j.evidence,
            )
            for j in self._maintenance.for_run(run.tenant_id, run.run_id)
            if j.kind == READBACK_KIND
        ]
        if self._record_presenter is not None:
            for i, view in enumerate(views):
                step = self._find_step(run.run_id, view.step_id)
                if (
                    step is not None
                    and view.plan_version == run.plan_version
                    and view.action_hash == step.action_hash
                ):
                    views[i] = view.model_copy(
                        update={
                            "record_evidence": self._record_presenter.project(
                                run, step, verification=view
                            )
                        }
                    )
        return views

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
        pending_evidence = self._maintenance is not None and any(
            not j.continued and j.kind == READBACK_KIND
            for j in self._maintenance.for_run(run.tenant_id, run_id)
        )
        if run.status not in RUN_TERMINAL or pending_evidence:
            self.kick(run_id)
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
        self.kick(decided.run_id)
        step = self._find_step(decided.run_id, decided.step_id)
        if step is None:
            raise NotFoundError("approval not found")
        return self._approval_view(decided, run, step)

    # -- hand-offs (M20) -----------------------------------------------------------------

    def handoffs(self, who: Identity) -> list[HandoffView]:
        """Prepared steps in this tenant that this person could mark right now."""
        views: list[HandoffView] = []
        waiting = self._runs.list_by_status(
            {RunStatus.WAITING_FOR_PERSON}, limit=_HANDOFF_SCAN_LIMIT
        )
        for run in waiting:
            if run.tenant_id != who.tenant_id or run.acting_user_id == who.user_id:
                continue
            for planned in run.plan.steps if run.plan else ():
                step = self._find_step(run.run_id, planned.id)
                if (
                    step is None
                    or step.status is not StepStatus.PREPARED
                    or step.handoff is None
                    or step.handoff.role not in who.roles
                ):
                    continue
                views.append(self._handoff_view(run, step))
        return views[: self._limits.max_list]

    def mark_handoff(
        self,
        who: Identity,
        run_id: str,
        step_id: str,
        *,
        done: bool,
        action_hash: str,
        reference: str | None = None,
    ) -> HandoffView:
        """Record that a person did (or didn't) carry out a prepared action, then let the run go
        on. Another tenant's run, or a step that was never handed off, is not found."""
        run = self._find_run(run_id)
        if run is None or run.tenant_id != who.tenant_id:
            raise NotFoundError("hand-off not found")
        step = self._find_step(run_id, step_id)
        if step is None or step.handoff is None:
            raise NotFoundError("hand-off not found")
        try:
            marked = self._orch.mark_handoff(
                run_id,
                step_id,
                user_id=who.user_id,
                roles=who.roles,
                done=done,
                action_hash=action_hash,
                reference=reference,
            )
        except HandoffError as exc:
            code = str(exc)
            if code in {"role_required", "requester_cannot_mark"}:
                raise ForbiddenError(code) from exc
            if code == "reference_invalid":
                raise BadRequestError(code) from exc
            raise ConflictError(code) from exc
        except StaleStateError as exc:
            raise ConflictError("not_waiting_for_a_person") from exc
        self.kick(run_id)
        return self._handoff_view(run, marked)

    # -- next-day review -----------------------------------------------------------------

    def review_items(
        self, who: Identity, *, status: Status | None = "open"
    ) -> list[ReviewItemView]:
        """The tenant's review items, for someone who may give a verdict."""
        self._require_reviewer(who)
        items = self._reviews.list(who.tenant_id, status=status, limit=self._limits.max_list)
        return [_review_view(i) for i in items]

    def review_verdict(self, who: Identity, item_id: str, *, verdict: Verdict) -> ReviewItemView:
        """Record `confirmed` or `missed_risk`, once, by a reviewer who did not start the run."""
        self._require_reviewer(who)
        try:
            item = self._reviews.get(who.tenant_id, item_id)
        except ReviewItemNotFoundError:
            raise NotFoundError("review item not found") from None
        run = self._find_run(item.run_id)
        if run is None or run.tenant_id != who.tenant_id:
            raise NotFoundError("review item not found")
        if run.acting_user_id == who.user_id:
            raise ForbiddenError("the person who started a run cannot review it")
        try:
            decided = self._reviews.decide(
                who.tenant_id,
                item_id,
                verdict=verdict,
                decided_by=who.user_id,
                decided_at=self._clock(),
            )
        except ReviewAlreadyDecidedError as exc:
            raise ConflictError(str(exc)) from exc
        return _review_view(decided)

    @staticmethod
    def _require_reviewer(who: Identity) -> None:
        if REVIEWER_ROLE not in who.roles:
            raise ForbiddenError(f"reviewing needs the {REVIEWER_ROLE} role")

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

    # -- step trace (M23) ----------------------------------------------------------------

    def step_trace(self, who: Identity, run_id: str, step_id: str) -> StepTraceView:
        """One step's records joined by hash and key, with the links that fail named as gaps."""
        run = self._own_run(who, run_id)
        planned = {s.id for s in run.plan.steps} if run.plan else set()
        step = self._find_step(run_id, step_id) if step_id in planned else None
        if step is None:
            raise NotFoundError("step not found")
        jobs = (
            [
                j
                for j in self._maintenance.for_run(run.tenant_id, run_id)
                if j.kind == READBACK_KIND and j.step_id == step_id
            ]
            if self._maintenance is not None
            else []
        )
        records = step_trace.StepRecords(
            run=run,
            step=step,
            approvals=self._approval_store.for_step(run_id, step_id),
            attempts=(
                self._executions.attempts(run_id, step_id) if self._executions is not None else []
            ),
            readback_jobs=jobs,
            events=self._audit.events(run_id),
            has_readback=(step.tool, step.tool_version) in self._readback_tools,
            conditions=self._checked_not_enforced.get((step.tool, step.tool_version), ()),
        )
        return step_trace.trace(records, self._signer)

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
        return resume_interrupted(self._runs, self.kick)

    def kick(self, run_id: str) -> None:
        """Drive the run on the background pool (resume, recovery, scheduled continuation)."""
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
            run = self._runs.get(run_id)
            if run.status in RUN_TERMINAL:
                self._anchorer.anchor_run(run)

    def _run_view(self, run: RunRecord) -> RunView:
        steps: list[StepView] = []
        evidence = self._verification_views(run)
        for planned in run.plan.steps if run.plan else ():
            step = self._find_step(run.run_id, planned.id)
            if step is None:
                continue
            decision = step.policy_decision or run.decisions.get(step.step_id)
            matched = next(
                (
                    v
                    for v in evidence
                    if v.step_id == step.step_id
                    and v.plan_version == run.plan_version
                    and v.action_hash == step.action_hash
                ),
                None,
            )
            steps.append(
                StepView(
                    record_evidence=(
                        matched.record_evidence
                        if matched
                        else self._record_presenter.project(run, step)
                        if self._record_presenter
                        else None
                    ),
                    step_id=step.step_id,
                    tool=step.tool,
                    status=step.status.value,
                    depends_on=step.depends_on,
                    policy_outcome=decision.outcome if decision else None,
                    policy_rule_ids=decision.rule_ids if decision else (),
                    policy_checks=tuple(
                        CheckView(rule_id=c.rule_id, path=c.path, op=c.op, unreadable=c.unreadable)
                        for c in (decision.checks if decision else ())
                    ),
                    approver_role=decision.approver_role if decision else None,
                    action_hash=step.action_hash,
                    error=step.error,
                    checked_not_enforced=self._checked_not_enforced.get(
                        (step.tool, step.tool_version), ()
                    ),
                    handoff=(
                        HandoffMarkView(
                            role=step.handoff.role,
                            marked_by=step.handoff.by,
                            marked_at=step.handoff.at,
                            reference=step.handoff.reference,
                        )
                        if step.handoff
                        else None
                    ),
                    verification=matched,
                )
            )
        return RunView(
            run_id=run.run_id,
            objective=run.objective,
            profile=run.profile,
            source_id=run.source_id,
            model_mode=run.model_mode,
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
        decision = step.policy_decision or run.decisions.get(step.step_id)
        manifest = self._registry.get(step.tool, step.tool_version)
        snapshot = record.resolved_args or step.resolved_args
        classes = manifest.field_classes or {}
        shown = fields(snapshot, classes)
        return ApprovalView(
            model_mode=run.model_mode,
            record_evidence=(
                self._record_presenter.project(run, step, args=snapshot, digest=record.action_hash)
                if self._record_presenter
                else None
            ),
            approval_id=record.approval_id,
            run_id=record.run_id,
            step_id=record.step_id,
            tool=step.tool,
            risk_tier=manifest.risk_tier,
            resolved_args=record.resolved_args or step.resolved_args,
            preparation=record.preparation,
            action_hash=record.action_hash,
            approver_role=record.approver_role,
            reason=decision.reason if decision else "",
            requested_by=record.requested_by,
            requested_at=record.requested_at,
            expires_at=record.expires_at,
            decision=record.decision,
            decided_by=record.decided_by,
            typed_fields=tuple(
                TypedFieldView(path=f.path, value=f.value) for f in shown if f.kind == "typed"
            ),
            free_text_fields=tuple(
                FreeTextFieldView(
                    path=f.path, label=FREE_TEXT_LABEL, length=length(f.value), content=f.value
                )
                for f in shown
                if f.kind == "free_text"
            ),
            difference=self._difference(record, run, step, snapshot, classes),
            history=tuple(
                HistoryItemView.model_validate(item)
                for item in record.preparation.get("history") or ()
            ),
        )

    def _handoff_view(self, run: RunRecord, step: StepRecord) -> HandoffView:
        assert step.handoff is not None and step.action_hash is not None
        decision = step.policy_decision or run.decisions.get(step.step_id)
        manifest = self._registry.get(step.tool, step.tool_version)
        shown = fields(step.resolved_args, manifest.field_classes or {})
        return HandoffView(
            run_id=run.run_id,
            step_id=step.step_id,
            tool=step.tool,
            risk_tier=manifest.risk_tier,
            resolved_args=step.resolved_args,
            preparation=step.preparation,
            action_hash=step.action_hash,
            role=step.handoff.role,
            policy_outcome=decision.outcome if decision else None,
            reason=decision.reason if decision else "",
            requested_by=run.acting_user_id,
            status=step.status.value,
            marked_by=step.handoff.by,
            marked_at=step.handoff.at,
            reference=step.handoff.reference,
            typed_fields=tuple(
                TypedFieldView(path=f.path, value=f.value) for f in shown if f.kind == "typed"
            ),
            free_text_fields=tuple(
                FreeTextFieldView(
                    path=f.path, label=FREE_TEXT_LABEL, length=length(f.value), content=f.value
                )
                for f in shown
                if f.kind == "free_text"
            ),
        )

    def _difference(
        self,
        record: ApprovalRecord,
        run: RunRecord,
        step: StepRecord,
        snapshot: Mapping[str, Any],
        classes: Mapping[str, str],
    ) -> DifferenceView:
        previous = self._approval_store.latest_approved(
            tenant_id=run.tenant_id,
            tool=step.tool,
            exclude_id=record.approval_id,
            decided_before=record.decided_at,
        )
        if previous is None:
            return DifferenceView()
        try:
            earlier = self._registry.get(step.tool, previous.tool_version).field_classes or {}
        except ToolNotFoundError:
            earlier = {}  # unknown version: every earlier value counts as free text
        changes = difference(snapshot, classes, previous.record.resolved_args, earlier)
        return DifferenceView(
            previous_approval_id=previous.record.approval_id,
            previous_decided_at=previous.record.decided_at,
            changes=tuple(
                ChangeView(
                    path=c.path, kind=c.kind, change=c.change, before=c.before, after=c.after
                )
                for c in changes
            ),
        )


def _review_view(item: ReviewItem) -> ReviewItemView:
    return ReviewItemView(
        item_id=item.item_id,
        day=item.day,
        run_id=item.run_id,
        step_id=item.step_id,
        action_hash=item.action_hash,
        reason=item.reason,
        key_id=item.key_id,
        status=item.status,
        created_at=item.created_at,
        decided_by=item.decided_by,
        decided_at=item.decided_at,
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
