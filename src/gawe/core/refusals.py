"""Read-only, bounded observations explaining a proven refusal; never retry authority."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from gawe.core.record_changes import (
    CapturedField,
    RecordObservation,
    RefusalDiagnostic,
    SnapshotSpec,
)
from gawe.core.runs import RunRecord

REFUSAL_CODES = frozenset({"record_changed", "event_changed"})
RecordRead = Callable[[str, RunRecord], tuple[Mapping[str, Any], str] | None]


def observe_record(
    spec: SnapshotSpec,
    subject: str,
    row: tuple[Mapping[str, Any], str] | None,
    *,
    timing: Literal["at_guard", "after_refusal"],
    now: datetime,
) -> RecordObservation:
    base: dict[str, Any] = dict(
        resource=spec.resource, subject=subject, observed_at=now, timing=timing
    )
    if row is None:
        return RecordObservation(**base, status="missing")
    try:
        record, revision = row
        observed = RecordObservation(
            **base,
            status="observed",
            revision=revision,
            fields=tuple(
                CapturedField(path=f.path, present=f.path in record, value=record.get(f.path))
                for f in spec.fields
            ),
        )
        spec.validate_fields(observed)
        return observed
    except Exception:
        return RecordObservation(**base, status="unavailable", reason="invalid")


@dataclass(frozen=True)
class RefusalObserver:
    spec: SnapshotSpec
    subjects: Callable[[Mapping[str, Any]], tuple[str, ...]]
    read: RecordRead
    revisions: Callable[[Mapping[str, Any]], Mapping[str, str]] | None = None

    def guard(
        self, args: Mapping[str, Any], observations: tuple[RecordObservation, ...]
    ) -> tuple[RecordObservation, ...]:
        """Reject extraneous/malformed evidence before persisting any of it."""
        subjects = set(self.subjects(args))
        seen: set[str] = set()
        validated: list[RecordObservation] = []
        for original in observations:
            observed = RecordObservation.model_validate(original.model_dump())
            if (
                observed.resource != self.spec.resource
                or observed.subject not in subjects
                or observed.subject in seen
                or observed.timing != "at_guard"
            ):
                raise ValueError("guard observation identity changed")
            seen.add(observed.subject)
            if observed.status == "observed":
                self.spec.validate_fields(observed)
            validated.append(observed)
        return tuple(validated)

    def diagnostic(
        self,
        args: Mapping[str, Any],
        run: RunRecord,
        digest: str,
        code: str,
        guard: tuple[RecordObservation, ...],
        clock: Callable[[], datetime],
    ) -> RefusalDiagnostic:
        subjects = self.subjects(args)
        if not subjects or len(subjects) > 200 or len(set(subjects)) != len(subjects):
            raise ValueError("invalid diagnostic targets")
        observations = list(self.guard(args, guard))
        if not observations:
            for subject in subjects:
                try:
                    row = self.read(subject, run)
                except Exception:
                    observations.append(
                        RecordObservation(
                            resource=self.spec.resource,
                            subject=subject,
                            observed_at=clock(),
                            timing="after_refusal",
                            status="unavailable",
                            reason="unreadable",
                        )
                    )
                else:
                    observations.append(
                        observe_record(self.spec, subject, row, timing="after_refusal", now=clock())
                    )
        diagnostic = RefusalDiagnostic(
            action_hash=digest,
            code=code,  # type: ignore[arg-type]
            coverage="complete" if len(observations) == len(subjects) else "partial",
            observations=tuple(observations),
        )
        diagnostic.validate_for(digest, {(self.spec.resource, s) for s in subjects})
        return diagnostic
