"""The step trace (M23): one step's records, joined by the hash or key that links them.

A step's records live in five places: the step itself (its action hash), its signed policy
decision (bound to that hash), its approvals (bound to it), its attempts (keyed by an operation
key derived from it), and its read-back job (carrying it); the audit trail holds a hash of each
event's payload. Nothing else joins them, so this does, and names each link that is missing or
doesn't match as a gap.

The only legitimate way a step's action hash changes is a recipient change (M16a): the step is
prepared again under a new hash and needs a fresh approval. Records bound to such an earlier hash
are shown and marked superseded, never a gap; the change leaves its own record (an attempt or a
read-back job ending `recipient_changed`), and that record is what makes a hash superseded.

Like the audit trail, the trace carries hashes, ids, codes and times only.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gawe.api.schemas import (
    HandoffMarkView,
    StepTraceView,
    TraceApprovalView,
    TraceAttemptView,
    TraceConditionView,
    TraceDecisionView,
    TraceReadbackView,
)
from gawe.core.approvals import SYSTEM_ACTOR, ApprovalRecord
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.executor import idempotency_key
from gawe.core.maintenance import MaintenanceJob, run_target
from gawe.core.models import AuditEvent, PolicyDecision
from gawe.core.policy.audit_payload import decided_payload
from gawe.core.policy.signing import PolicySigner
from gawe.core.readback import operation_identity
from gawe.core.records import ExecutionRecord, StepRecord
from gawe.core.runs import RunRecord
from gawe.core.statemachine import StepStatus

RECIPIENT_CHANGED = "recipient_changed"
_BEFORE_POLICY = frozenset({StepStatus.PENDING, StepStatus.READY, StepStatus.POLICY_CHECKED})
_HANDED_OFF = frozenset(
    {StepStatus.PREPARED, StepStatus.DONE_BY_PERSON, StepStatus.NOT_DONE_BY_PERSON}
)


@dataclass(frozen=True)
class StepRecords:
    """Everything stored about one step, read by the caller from its stores."""

    run: RunRecord
    step: StepRecord
    approvals: Sequence[ApprovalRecord]
    attempts: Sequence[ExecutionRecord]
    readback_jobs: Sequence[MaintenanceJob]  # this step's read-back jobs, any hash
    events: Sequence[AuditEvent]  # the run's audit trail
    has_readback: bool  # the tool version is a write read back from its target
    conditions: tuple[str, ...]  # names of the dispatch conditions it's checked against


def payload_hash(payload: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json(payload))


def trace(records: StepRecords, signer: PolicySigner | None) -> StepTraceView:
    run, step = records.run, records.step
    current = step.action_hash
    audited = Counter((e.type, e.payload_hash) for e in records.events)
    plan_versions = {run.plan_version} | {run_target(j).plan_version for j in records.readback_jobs}

    def keys(h: str) -> set[str]:
        return {idempotency_key(h)} | {operation_identity(h, v) for v in plan_versions}

    superseded = _superseded(records, current, keys)
    known = ({current} if current else set()) | set(superseded)
    key_hash = {k: h for h in known for k in keys(h)}
    gaps: list[str] = []

    # The decision.
    decision = step.policy_decision or run.decisions.get(step.step_id)
    decision_view = None
    if decision is None:
        if current is not None and step.status not in _BEFORE_POLICY:
            gaps.append("decision_missing")
    else:
        decision_view = _decision_view(decision, signer, audited)
        if decision.step_id != step.step_id:
            gaps.append("decision_for_other_step")
        if decision.action_hash != current:
            gaps.append("decision_hash_mismatch")
        if decision_view.signature_valid is False:
            gaps.append("decision_signature_invalid")
        if not decision_view.audited:
            gaps.append("decision_not_audited")

    # The approvals.
    approval_views = tuple(_approval_view(a, current, audited) for a in records.approvals)
    if any(a.action_hash not in known for a in records.approvals):
        gaps.append("approval_hash_mismatch")
    if any(not a.audited for a in approval_views):
        gaps.append("approval_not_audited")
    needs_approval = decision is not None and decision.outcome == "needs_approval"
    if (
        needs_approval
        and step.handoff is None
        and step.status not in _BEFORE_POLICY
        and not any(a.current for a in approval_views)
    ):
        gaps.append("approval_missing")

    # The attempts.
    attempt_views = tuple(
        TraceAttemptView(
            attempt=a.attempt,
            status=a.status,
            operation_key=a.idempotency_key,
            action_hash=key_hash.get(a.idempotency_key),
            error=a.error,
            output_hash=a.output_hash,
            started_at=a.started_at,
            finished_at=a.finished_at,
        )
        for a in records.attempts
    )
    if any(a.action_hash is None for a in attempt_views):
        gaps.append("attempt_key_mismatch")
    dispatched = [a for a in attempt_views if current is not None and a.action_hash == current]
    if dispatched:
        if decision is not None and decision.outcome == "deny":
            gaps.append("dispatched_after_denial")
        approved = any(a.current and a.decision == "approved" for a in approval_views)
        if needs_approval and not approved:
            gaps.append("dispatched_without_approval")
        if step.handoff is not None:
            gaps.append("dispatched_after_handoff")

    # Conditions checked just before dispatch (M22), matched to this step by payload hash.
    condition_views = _conditions(step, records.conditions, audited)
    if records.conditions and dispatched and not condition_views:
        gaps.append("condition_not_checked")

    # The hand-off (M20).
    if step.status in _HANDED_OFF and step.handoff is None:
        gaps.append("handoff_missing")

    # The read-back (M16a).
    readback_views = tuple(
        TraceReadbackView(
            job_id=j.job_id,
            operation_key=j.operation_key,
            plan_version=run_target(j).plan_version,
            action_hash=run_target(j).action_hash,
            current=(
                run_target(j).action_hash == current
                and run_target(j).plan_version == run.plan_version
            ),
            outcome=j.result.get("outcome") if j.result else None,
            code=j.result.get("code") if j.result else j.evidence.get("code"),
        )
        for j in records.readback_jobs
    )
    if any(r.action_hash not in known for r in readback_views):
        gaps.append("readback_hash_mismatch")
    if any(r.action_hash == current and not r.current for r in readback_views):
        gaps.append("readback_plan_mismatch")
    for r in readback_views:
        if r.action_hash in known and r.operation_key not in keys(r.action_hash):
            gaps.append("readback_key_mismatch")
            break
    if records.has_readback and dispatched and not any(r.current for r in readback_views):
        gaps.append("readback_missing")

    return StepTraceView(
        run_id=run.run_id,
        step_id=step.step_id,
        plan_version=run.plan_version,
        tool=step.tool,
        tool_version=step.tool_version,
        status=step.status.value,
        error=step.error,
        args_hash=payload_hash(step.resolved_args) if current is not None else None,
        action_hash=current,
        superseded_hashes=tuple(sorted(superseded)),
        decision=decision_view,
        approvals=approval_views,
        attempts=attempt_views,
        conditions=condition_views,
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
        readback=readback_views,
        gaps=tuple(gaps),
    )


def _superseded(records: StepRecords, current: str | None, keys: Any) -> set[str]:
    """Earlier hashes of this step that a recorded recipient change replaced."""
    candidates = {a.action_hash for a in records.approvals}
    candidates |= {run_target(j).action_hash for j in records.readback_jobs}
    candidates.discard(current or "")
    found: set[str] = set()
    for h in candidates:
        by_job = any(
            run_target(j).action_hash == h and (j.result or {}).get("outcome") == RECIPIENT_CHANGED
            for j in records.readback_jobs
        )
        by_attempt = any(
            a.idempotency_key in keys(h) and a.error == RECIPIENT_CHANGED for a in records.attempts
        )
        if by_job or by_attempt:
            found.add(h)
    return found


def _decision_view(
    decision: PolicyDecision, signer: PolicySigner | None, audited: Counter[tuple[str, str]]
) -> TraceDecisionView:
    return TraceDecisionView(
        outcome=decision.outcome,
        rule_ids=decision.rule_ids,
        approver_role=decision.approver_role,
        action_hash=decision.action_hash,
        signature_valid=None if signer is None else signer.verify(decision),
        audited=audited[("policy.decided", payload_hash(decided_payload(decision)))] > 0,
    )


def _approval_view(
    record: ApprovalRecord, current: str | None, audited: Counter[tuple[str, str]]
) -> TraceApprovalView:
    # The same payloads `ApprovalService` records; a system closure has its own event.
    requested = {
        "approval_id": record.approval_id,
        "step_id": record.step_id,
        "action_hash": record.action_hash,
        "approver_role": record.approver_role,
        "expires_at": record.expires_at.isoformat(),
    }
    ok = audited[("approval.requested", payload_hash(requested))] > 0
    if record.decision is not None and record.decided_by != SYSTEM_ACTOR:
        decided = {
            "approval_id": record.approval_id,
            "step_id": record.step_id,
            "action_hash": record.action_hash,
            "decision": record.decision,
            "comment_sha256": (
                None if record.comment is None else sha256_hex(canonical_json(record.comment))
            ),
        }
        ok = ok and audited[("approval.decided", payload_hash(decided))] > 0
    return TraceApprovalView(
        approval_id=record.approval_id,
        action_hash=record.action_hash,
        current=record.action_hash == current,
        approver_role=record.approver_role,
        requested_by=record.requested_by,
        requested_at=record.requested_at,
        decision=record.decision,
        decided_by=record.decided_by,
        decided_at=record.decided_at,
        audited=ok,
    )


def _conditions(
    step: StepRecord, names: Iterable[str], audited: Counter[tuple[str, str]]
) -> tuple[TraceConditionView, ...]:
    """Count the step's `condition.checked` events by result. The audit keeps only a payload
    hash, so each possible payload for this step and hash is rebuilt and looked up."""
    names = list(names)
    if not names or step.action_hash is None:
        return ()
    results = ["held"] + [f"{kind}:{n}" for n in names for kind in ("changed", "unreadable")]
    views = []
    for result in results:
        payload = {
            "step_id": step.step_id,
            "action_hash": step.action_hash,
            "conditions": names,
            "result": result,
        }
        count = audited[("condition.checked", payload_hash(payload))]
        if count:
            views.append(TraceConditionView(result=result, count=count))
    return tuple(views)
