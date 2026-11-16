"""A generic maintenance runner: claim due jobs, hand each to its kind's handler, report.

`tick(now)` is the whole interface. A kind whose handler can schedule its own jobs (the daily
review) creates them first, by deterministic id, so racing runners add each job once. One tick
then pages through due jobs in `(due_at, job_id)` order with an exclusive cursor, claims each
with a short lease by compare-and-set on its revision, and calls the handler registered for the
job's kind. A lost claim means another runner owns the job;
it is counted as contended, never processed twice. A handler failure is contained to its job:
the job's lease is released and its due time moved back one interval, so a job that keeps failing
goes to the back of the queue instead of starving later work.

Every tick ends with one heartbeat per registered kind: a structured `gawe.maintenance.heartbeat`
log record plus telemetry counters and backlog observations. A tick that fails as a whole (the
store is unreachable, say) emits an error heartbeat, never a successful one with zero counts.
Heartbeats carry counts, timings, kinds and an error class name only: no recipients, arguments,
bodies or tenant data.

Alarm rules (see `assess`): the unknown backlog growing between the last two successful
heartbeats is one condition; a missing, stale or failing heartbeat is a separate one, so a stopped
scheduler cannot look like a healthy one with nothing to do. For a kind whose handler reports
overdue work (grants past their end date that nothing revoked), a count above zero in the last two
successful heartbeats is a third.
"""

import json
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, runtime_checkable
from uuid import uuid4

from gawe.core.maintenance import JobConflictError, MaintenanceJob, MaintenanceStore
from gawe.core.telemetry import NoopTelemetry, Telemetry

log = logging.getLogger("gawe.maintenance")

HEARTBEAT_EVENT = "gawe.maintenance.heartbeat"
DEFAULT_LEASE = timedelta(seconds=30)

# Labels a handler may return; the heartbeat reports each one, zero when absent.
OUTCOMES = (
    "done",  # resolved this tick: fresh read agrees
    "blocked",  # resolved this tick: fresh read disagrees
    "newly_unknown",  # resolved this tick: deadline reached without a usable read
    "failed",  # resolved this tick: protocol failure or proven non-dispatch
    "refreshed",  # resolved this tick: recipients changed before dispatch
    "still_waiting",  # no usable read yet; due again before its deadline
    "continued",  # an earlier result's step/run continuation was acknowledged
    "pending_continuation",  # a result exists but its run could not be settled yet
    "skipped",  # nothing this handler may do (no planning, identity mismatch)
    "revoked",  # resolved this tick: an expired grant reads back as revoked
    "not_granted",  # resolved this tick: the grant was never accepted, nothing to revoke
    "reviewed",  # a tenant's daily review ran and recorded its items
)


class JobHandler(Protocol):
    kind: str

    def process(self, job: MaintenanceJob) -> str:
        """Process one claimed job (its lease is held by `job.lease_owner`); return an outcome."""
        ...


@runtime_checkable
class Scheduler(Protocol):
    def schedule(self, now: datetime) -> int:
        """Create this kind's due jobs (idempotently); return how many were new."""
        ...


@runtime_checkable
class OverdueProbe(Protocol):
    def overdue(self, now: datetime) -> int:
        """Work past due that is still not done, read from the target systems."""
        ...


@dataclass(frozen=True)
class Heartbeat:
    kind: str
    status: Literal["ok", "error"]
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    checked: int = 0
    outcomes: Mapping[str, int] = field(default_factory=dict)
    errors: int = 0
    contended: int = 0
    scheduled: int = 0  # jobs this kind's scheduler created this tick
    unresolved_total: int | None = None  # still waiting for a usable read (any due time)
    unknown_total: int | None = None  # terminal unknown, retained after acknowledgment
    overdue_active: int | None = None  # kinds with an overdue probe: past due and not done
    error: str | None = None

    def fields(self) -> dict[str, object]:
        data = asdict(self)
        data["started_at"] = self.started_at.isoformat()
        data["finished_at"] = self.finished_at.isoformat()
        data["outcomes"] = {name: self.outcomes.get(name, 0) for name in OUTCOMES}
        return {"event": HEARTBEAT_EVENT, **data}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MaintenanceRunner:
    def __init__(
        self,
        *,
        store: MaintenanceStore,
        handlers: Sequence[JobHandler],
        telemetry: Telemetry | None = None,
        clock: Callable[[], datetime] = _utc_now,
        batch_size: int = 50,
        max_jobs: int = 500,
        retry_after: timedelta = timedelta(seconds=30),
        lease: timedelta = DEFAULT_LEASE,
        history: int = 32,
    ) -> None:
        kinds = [h.kind for h in handlers]
        if len(set(kinds)) != len(kinds):
            raise ValueError("one handler per job kind")
        if batch_size < 1 or max_jobs < 1:
            raise ValueError("batch_size and max_jobs must be positive")
        self._store = store
        self._handlers = {h.kind: h for h in handlers}
        self._telemetry: Telemetry = telemetry or NoopTelemetry()
        self._clock = clock
        self._batch = batch_size
        self._max = max_jobs
        self._retry_after = retry_after
        self._lease = lease
        self._recent: deque[Heartbeat] = deque(maxlen=history)
        self._lock = threading.Lock()

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def recent(self) -> list[Heartbeat]:
        with self._lock:
            return list(self._recent)

    def tick(self, now: datetime | None = None) -> list[Heartbeat]:
        """One bounded pass over due work. Never raises an ordinary exception."""
        now = now or self._clock()
        started, start = self._clock(), time.monotonic()
        stats = {kind: Counter[str]() for kind in self._handlers}
        try:
            for kind, handler in self._handlers.items():
                if isinstance(handler, Scheduler):
                    stats[kind]["scheduled"] += handler.schedule(now)
            self._pass(now, stats)
            backlog = {kind: self._store.counts(kind) for kind in self._handlers}
            overdue = {
                kind: handler.overdue(now)
                for kind, handler in self._handlers.items()
                if isinstance(handler, OverdueProbe)
            }
        except Exception as exc:  # the tick as a whole failed: say so, never report zero work
            log.exception("maintenance tick failed")
            beats = [
                Heartbeat(
                    kind=kind,
                    status="error",
                    started_at=started,
                    finished_at=self._clock(),
                    duration_seconds=time.monotonic() - start,
                    checked=stats[kind]["checked"],
                    outcomes={k: v for k, v in stats[kind].items() if k in OUTCOMES},
                    errors=stats[kind]["error"],
                    contended=stats[kind]["contended"],
                    scheduled=stats[kind]["scheduled"],
                    error=type(exc).__name__,
                )
                for kind in self._handlers
            ]
        else:
            beats = [
                Heartbeat(
                    kind=kind,
                    status="ok",
                    started_at=started,
                    finished_at=self._clock(),
                    duration_seconds=time.monotonic() - start,
                    checked=stats[kind]["checked"],
                    outcomes={k: v for k, v in stats[kind].items() if k in OUTCOMES},
                    errors=stats[kind]["error"],
                    contended=stats[kind]["contended"],
                    scheduled=stats[kind]["scheduled"],
                    unresolved_total=backlog[kind].get("unresolved", 0),
                    unknown_total=backlog[kind].get("unknown", 0),
                    overdue_active=overdue.get(kind),
                )
                for kind in self._handlers
            ]
        for beat in beats:
            self._emit(beat)
        return beats

    def health(self, now: datetime, *, stale_after: timedelta) -> dict[str, "Health"]:
        beats = self.recent()
        return {
            kind: assess([b for b in beats if b.kind == kind], now=now, stale_after=stale_after)
            for kind in self._handlers
        }

    # -- one pass -------------------------------------------------------------------------

    def _pass(self, now: datetime, stats: Mapping[str, Counter[str]]) -> None:
        processed = 0
        cursor: tuple[datetime, str] | None = None
        while processed < self._max:
            limit = min(self._batch, self._max - processed)
            page = self._store.due(now, limit=limit, after=cursor)
            for job in page:
                cursor = (job.due_at, job.job_id)
                handler = self._handlers.get(job.kind)
                if handler is None:
                    continue  # another deployment's job kind; never claimed here
                processed += 1
                counter = stats[job.kind]
                counter["checked"] += 1
                claimed = self._claim(job, now)
                if claimed is None:
                    counter["contended"] += 1
                    continue
                try:
                    outcome = handler.process(claimed)
                except JobConflictError:
                    outcome = "contended"  # a competing owner moved the job on first
                except Exception:
                    log.exception("maintenance job %s (%s) failed", job.job_id, job.kind)
                    outcome = "error"
                if outcome not in {*OUTCOMES, "contended", "error"}:
                    outcome = "error"
                counter[outcome] += 1
                self._release(claimed, now, outcome)
            if len(page) < limit:
                return

    def _claim(self, job: MaintenanceJob, now: datetime) -> MaintenanceJob | None:
        claimed = MaintenanceJob(
            **{
                **job.model_dump(),
                "lease_owner": uuid4().hex,
                "lease_until": now + self._lease,
                "revision": job.revision + 1,
            }
        )
        try:
            return self._store.replace(claimed, expected_revision=job.revision)
        except JobConflictError:
            return None

    def _release(self, claimed: MaintenanceJob, now: datetime, outcome: str) -> None:
        """Drop our lease if we still hold it; unfinished work goes back one interval.

        The resolver itself clears the lease when it records a read, so a job may come back
        unleased. A result still awaiting continuation is moved back then too; an unresolved job
        keeps the next read time the resolver chose. Another runner's lease is never touched.
        Moving an unresolved job never passes its deadline, so expiry is not delayed by failures.
        """
        try:
            current = self._store.get(claimed.tenant_id, claimed.job_id)
            if current is None:
                return
            ours = current.lease_owner == claimed.lease_owner
            if current.lease_owner is not None and not ours:
                return  # another runner took over after our lease expired
            updates: dict[str, object] = {}
            if ours:
                updates.update(lease_owner=None, lease_until=None)
            if (
                not current.continued
                and outcome != "contended"
                and (ours or current.result is not None)
            ):
                later = now + self._retry_after
                if current.result is None:
                    later = min(later, current.deadline)
                updates["due_at"] = max(current.due_at, later)
            if not updates:
                return
            released = MaintenanceJob(
                **{**current.model_dump(), **updates, "revision": current.revision + 1}
            )
            self._store.replace(released, expected_revision=current.revision)
        except JobConflictError:
            return
        except Exception:
            log.exception("could not release maintenance job %s", claimed.job_id)

    # -- signals --------------------------------------------------------------------------

    def _emit(self, beat: Heartbeat) -> None:
        with self._lock:
            self._recent.append(beat)
        fields = beat.fields()
        level = logging.INFO if beat.status == "ok" else logging.ERROR
        log.log(
            level,
            "%s %s",
            HEARTBEAT_EVENT,
            json.dumps(fields, sort_keys=True),
            extra={"heartbeat": fields},
        )
        t = self._telemetry
        t.count(HEARTBEAT_EVENT, kind=beat.kind, status=beat.status)
        t.observe(
            "gawe.maintenance.tick_seconds",
            beat.duration_seconds,
            kind=beat.kind,
            status=beat.status,
        )
        if beat.checked:
            t.count("gawe.maintenance.checked", beat.checked, kind=beat.kind)
        for name, value in beat.outcomes.items():
            if value:
                t.count("gawe.maintenance.jobs", value, kind=beat.kind, outcome=name)
        for name, value in (("error", beat.errors), ("contended", beat.contended)):
            if value:
                t.count("gawe.maintenance.jobs", value, kind=beat.kind, outcome=name)
        if beat.status == "ok":
            assert beat.unresolved_total is not None and beat.unknown_total is not None
            t.observe(
                "gawe.maintenance.backlog", beat.unresolved_total, kind=beat.kind, state="waiting"
            )
            t.observe(
                "gawe.maintenance.backlog", beat.unknown_total, kind=beat.kind, state="unknown"
            )
            if beat.overdue_active is not None:
                t.observe(
                    "gawe.maintenance.backlog",
                    beat.overdue_active,
                    kind=beat.kind,
                    state="overdue_active",
                )


@dataclass(frozen=True)
class Health:
    heartbeat: Literal["ok", "missing", "stale", "error"]
    unknown_growing: bool
    last_heartbeat_at: datetime | None = None
    overdue_active: bool = False


def assess(beats: Sequence[Heartbeat], *, now: datetime, stale_after: timedelta) -> Health:
    """Evaluate one kind's heartbeats (oldest first) against the two alarm conditions.

    * `unknown_growing`: the unknown backlog is larger at the latest successful heartbeat than
      at the successful heartbeat before it. Terminal unknowns are retained in the backlog, so
      growth means new unknowns, not churn.
    * `overdue_active`: the latest two successful heartbeats both report overdue work (a grant
      past its end date that nothing revoked). One heartbeat alone may just precede the sweep.
    * `heartbeat`: `missing` (none at all), `stale` (latest older than `stale_after`), `error`
      (the latest tick failed) or `ok`. A stale or missing heartbeat is the stopped-scheduler
      condition; it is reported separately so silence is never mistaken for a healthy zero.
    """
    if not beats:
        return Health("missing", unknown_growing=False)
    ordered = sorted(beats, key=lambda b: b.finished_at)
    latest = ordered[-1]
    ok = [b for b in ordered if b.status == "ok"]
    growing = (
        len(ok) >= 2
        and ok[-1].unknown_total is not None
        and ok[-2].unknown_total is not None
        and ok[-1].unknown_total > ok[-2].unknown_total
    )
    overdue = len(ok) >= 2 and all((b.overdue_active or 0) > 0 for b in ok[-2:])
    state: Literal["ok", "missing", "stale", "error"]
    if now - latest.finished_at > stale_after:
        state = "stale"
    elif latest.status == "error":
        state = "error"
    else:
        state = "ok"
    return Health(
        state,
        unknown_growing=growing,
        last_heartbeat_at=latest.finished_at,
        overdue_active=overdue,
    )
