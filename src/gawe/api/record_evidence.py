"""Safe display projection of persisted record evidence. Never reads a live target."""

from collections.abc import Mapping
from contextlib import suppress
from typing import Any, Literal

from gawe.api.schemas import RecordEvidenceView, RecordTargetView, VerificationView
from gawe.core.canonical import action_hash
from gawe.core.record_changes import RecordSnapshot, RefusalDiagnostic, compare_records
from gawe.core.records import StepRecord
from gawe.core.refusals import REFUSAL_CODES, RefusalObserver
from gawe.core.registry import ToolRegistry
from gawe.core.runs import RunRecord
from gawe.core.undo import UndoStore, restore_args, reversal_run_id


class RecordPresenter:
    def __init__(
        self,
        registry: ToolRegistry,
        observers: Mapping[tuple[str, str], RefusalObserver],
        undo: UndoStore,
    ) -> None:
        self.registry, self.observers, self.undo = registry, dict(observers), undo

    def project(
        self,
        run: RunRecord,
        step: StepRecord,
        *,
        args: Mapping[str, Any] | None = None,
        digest: str | None = None,
        verification: VerificationView | None = None,
    ) -> RecordEvidenceView | None:
        observer = self.observers.get((step.tool, step.tool_version))
        digest = digest or step.action_hash
        if observer is None or digest is None:
            return None
        args = step.resolved_args if args is None else args
        error = step.error or (verification.code if verification else None)
        code: Literal["record_changed", "event_changed"] | None = None
        if error == "record_changed":
            code = "record_changed"
        elif error == "event_changed":
            code = "event_changed"
        empty = RecordEvidenceView(action_hash=digest, code=code)
        try:
            if digest != step.action_hash or digest != action_hash(
                tool=step.tool,
                version=step.tool_version,
                resolved_args=args,
                run_id=run.run_id,
                step_id=step.step_id,
            ):
                return empty
            subjects = observer.subjects(args)
            bindings = observer.revisions(args) if observer.revisions else {}
            if (
                not 1 <= len(subjects) <= 200
                or len(set(subjects)) != len(subjects)
                or set(bindings) != set(subjects)
            ):
                return empty
            if any(
                not isinstance(s, str)
                or not 1 <= len(s) <= 256
                or not isinstance(bindings[s], str)
                or not 1 <= len(bindings[s]) <= 128
                for s in subjects
            ):
                return empty
            manifest = self.registry.get(step.tool, step.tool_version)
        except Exception:
            return empty
        snapshots: dict[str, RecordSnapshot] = {}
        try:
            raw = (
                args.get("snapshots", [])
                if "snapshots" in manifest.input_schema["properties"]
                else []
            )
            if not isinstance(raw, list) or len(raw) > 200:
                raise ValueError("invalid snapshots")
            parsed = tuple(RecordSnapshot.model_validate(s) for s in raw)
            if parsed and (
                {s.subject for s in parsed} != set(subjects) or len(parsed) != len(subjects)
            ):
                raise ValueError("snapshot targets changed")
            if manifest.trusted_only and parsed:
                record = self.undo.get(run.tenant_id, args.get("undo_id", ""))
                if (
                    record is None
                    or record.state not in {"requested", "used", "failed"}
                    or run.profile != "undo"
                    or reversal_run_id(record) != run.run_id
                    or restore_args(record, include_snapshots=True) != args
                ):
                    raise ValueError("original provenance unavailable")
            for snapshot in parsed:
                observer.spec.validate_fields(snapshot)
                if snapshot.resource != observer.spec.resource:
                    raise ValueError("snapshot resource changed")
                if not manifest.trusted_only and (
                    snapshot.basis != "preparation"
                    or snapshot.revision != bindings[snapshot.subject]
                ):
                    raise ValueError("snapshot revision changed")
                snapshots[snapshot.subject] = snapshot
        except Exception:
            snapshots = {}
        diagnostic = None
        if code in REFUSAL_CODES and verification is not None:
            with suppress(Exception):  # Bad evidence is unavailable, never an empty comparison.
                if verification.code != code or verification.outcome != "not_dispatched":
                    raise ValueError("verification outcome changed")
                if (
                    verification.action_hash != digest
                    or verification.step_id != step.step_id
                    or verification.plan_version != run.plan_version
                ):
                    raise ValueError("verification identity changed")
                candidate = RefusalDiagnostic.model_validate(
                    verification.evidence.get("diagnostic")
                )
                candidate.validate_for(digest, {(observer.spec.resource, s) for s in subjects})
                if candidate.code != code:
                    raise ValueError("refusal code changed")
                for entry in candidate.observations:
                    if entry.status == "observed":
                        observer.spec.validate_fields(entry)
                diagnostic = candidate
        observed_by_subject = {o.subject: o for o in diagnostic.observations} if diagnostic else {}
        records = []
        for subject in subjects:
            before, observed = snapshots.get(subject), observed_by_subject.get(subject)
            comparison = compare_records(observer.spec, before, observed)
            records.append(
                RecordTargetView(
                    resource=observer.spec.resource,
                    subject=subject,
                    guard_revision=bindings[subject],
                    state=(
                        ("captured" if observer.spec.fields else "no_fields_tracked")
                        if code is None and before is not None
                        else comparison.state
                    ),
                    snapshot=before,
                    observation=observed,
                    rows=comparison.rows,
                )
            )
        return RecordEvidenceView(
            action_hash=digest,
            code=code,
            coverage=diagnostic.coverage if diagnostic else "unavailable",
            records=tuple(records),
        )
