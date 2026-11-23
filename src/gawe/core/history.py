"""An app's own history, for rules that only escalate (M24).

A rule may look at what the agent did before (a contract's last rating, how often a person was
granted a system) but only to add a review or a denial, never to remove one: startup refuses a
history variable in an `allow` rule. History is read from GAWE's own step records, which only the
engine writes and whose arguments each step's signed decision is bound to, never from a target
system's records, which tools write.

What counts as an earlier action: every step of the same tool (any version) in the same tenant,
naming the same subject, that is past policy and may have taken effect or still may: everything
but `denied`, `rejected` and `not_done_by_person`, and never a step that failed before policy.
Counting a failed or unknown write adds reviews, never removes one. In the same run only steps
earlier in the plan count; in live evaluation every other run counts, in-flight ones included,
and with `as_of` (the daily review, judging an old decision again) only runs started by then.

Two runs judging the same subject at once must not both see "nothing before": the orchestrator
holds a lock per (tenant, tool, subject) while it reads history, evaluates and records the
decision, so the step being judged is in flight before the next reader looks.
"""

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from gawe.core.records import InMemoryStepStore, StepRecord
from gawe.core.runs import RunNotFoundError, RunRecord, RunStore
from gawe.core.statemachine import StepStatus

HISTORY_VARIABLE = "history"
SHOWN_LIMIT = 10  # the approval card shows the latest few; the audit event counts them all
NOT_COUNTED = frozenset(
    {
        StepStatus.PENDING,
        StepStatus.READY,
        StepStatus.DENIED,
        StepStatus.REJECTED,
        StepStatus.NOT_DONE_BY_PERSON,
    }
)


def counts(step: StepRecord) -> bool:
    """Whether a step may have taken effect or still may."""
    return step.status not in NOT_COUNTED and step.action_hash is not None


@dataclass(frozen=True)
class HistoryEntry:
    run_id: str
    step_id: str
    tool_version: str
    action_hash: str
    status: StepStatus
    resolved_args: Mapping[str, Any]
    started_at: datetime  # when its run started


@dataclass(frozen=True)
class HistorySubject:
    """What an app declares for one tool: the argument naming the subject, and what its rules
    read from the earlier actions.

    `variables(entries, args, run)` returns the step's `history.*` variables: plain values a rule
    compares the step's arguments with. Entries come oldest first, so the last is the latest.
    `shown` names the arguments of each earlier action the approver sees; startup checks each is
    a typed field of every current version, so no model-written text reaches the card this way.
    """

    argument: str
    variables: Callable[[Sequence[HistoryEntry], Mapping[str, Any], RunRecord], Mapping[str, Any]]
    lower: bool = False
    shown: tuple[str, ...] = ()

    def subject(self, args: Mapping[str, Any]) -> str | None:
        value = args.get(self.argument)
        if not isinstance(value, str) or not value:
            return None
        return value.lower() if self.lower else value


class HistoryReader(Protocol):
    def earlier(
        self, tenant_id: str, tool: str, subject: HistorySubject, value: str
    ) -> list[HistoryEntry]:
        """Every counted step of `tool` in the tenant naming `value`, oldest run first. The
        caller removes the step being judged, later steps of its run and runs after `as_of`."""
        ...


class SubjectLocks(Protocol):
    def hold(self, key: str) -> AbstractContextManager[None]: ...


class InMemoryHistoryReader:
    def __init__(self, runs: RunStore, steps: InMemoryStepStore) -> None:
        self._runs, self._steps = runs, steps

    def earlier(
        self, tenant_id: str, tool: str, subject: HistorySubject, value: str
    ) -> list[HistoryEntry]:
        found: list[HistoryEntry] = []
        for step in self._steps.all_for_tool(tool):
            if not counts(step) or subject.subject(step.resolved_args) != value:
                continue
            try:
                run = self._runs.get(step.run_id)
            except RunNotFoundError:
                continue
            if run.tenant_id == tenant_id:
                found.append(entry(step, run.created_at))
        return sorted(found, key=lambda e: (e.started_at, e.run_id, e.step_id))


def entry(step: StepRecord, started_at: datetime) -> HistoryEntry:
    assert step.action_hash is not None
    return HistoryEntry(
        run_id=step.run_id,
        step_id=step.step_id,
        tool_version=step.tool_version,
        action_hash=step.action_hash,
        status=step.status,
        resolved_args=step.resolved_args,
        started_at=started_at,
    )


class InMemorySubjectLocks:
    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def hold(self, key: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            yield


@dataclass(frozen=True)
class HistoryRead:
    entries: tuple[HistoryEntry, ...]
    variables: Mapping[str, Any]
    shown: tuple[Mapping[str, Any], ...]

    def audit_payload(self, step_id: str, tool: str) -> dict[str, Any]:
        """References only: which earlier steps counted, never their arguments."""
        return {
            "step_id": step_id,
            "tool": tool,
            "count": len(self.entries),
            "counted": [[e.run_id, e.step_id, e.action_hash] for e in self.entries],
        }


class History:
    """An app's history declarations with a reader and the subject locks."""

    def __init__(
        self,
        subjects: Mapping[str, HistorySubject],
        reader: HistoryReader,
        locks: SubjectLocks,
    ) -> None:
        self._subjects, self._reader, self._locks = dict(subjects), reader, locks

    def subject(self, tool: str, args: Mapping[str, Any]) -> str | None:
        declared = self._subjects.get(tool)
        return declared.subject(args) if declared is not None else None

    def declared(self, tool: str) -> bool:
        return tool in self._subjects

    def hold(self, tenant_id: str, tool: str, value: str) -> AbstractContextManager[None]:
        return self._locks.hold(f"{tenant_id}\x1f{tool}\x1f{value}")

    def read(
        self,
        run: RunRecord,
        step_id: str,
        tool: str,
        args: Mapping[str, Any],
        *,
        as_of: datetime | None = None,
    ) -> HistoryRead:
        """The earlier actions for this step and the variables its rules read. Raises when the
        subject is missing or history can't be read; the caller leaves the variables out, so the
        step denies."""
        declared = self._subjects[tool]
        value = declared.subject(args)
        if value is None:
            raise HistoryUnreadableError("history subject missing")
        order = [s.id for s in run.plan.steps] if run.plan else []
        before = order[: order.index(step_id)] if step_id in order else []
        found = self._reader.earlier(run.tenant_id, tool, declared, value)
        others = [
            e for e in found if e.run_id != run.run_id and (as_of is None or e.started_at <= as_of)
        ]
        # This run's earlier steps come last, in plan order: they are the latest this run knows.
        mine = sorted(
            (e for e in found if e.run_id == run.run_id and e.step_id in before),
            key=lambda e: before.index(e.step_id),
        )
        entries = (*others, *mine)
        return HistoryRead(
            entries=entries,
            variables=dict(declared.variables(entries, args, run)),
            shown=tuple(shown_item(tool, e, declared.shown) for e in entries[-SHOWN_LIMIT:]),
        )


def shown_item(tool: str, entry: HistoryEntry, names: Sequence[str]) -> dict[str, Any]:
    """One earlier action as the approver sees it. JSON values only: it is stored with the step."""
    return {
        "tool": tool,
        "run_id": entry.run_id,
        "step_id": entry.step_id,
        "run_started_at": entry.started_at.isoformat(),
        "values": {n: entry.resolved_args[n] for n in names if n in entry.resolved_args},
    }


class HistoryUnreadableError(Exception):
    pass
