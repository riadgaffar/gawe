"""Assembles the engine and the API service from parts.

An application (Contract Review, the IT-access fixture) supplies an `AppBundle`: its tools, their
manifests, its policy rules and its Reader profile. Everything else is common. Stores come in
two kinds: in-memory (local demos and tests) and Postgres (everything survives a restart).
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gawe.api.background import Background
from gawe.api.record_evidence import RecordPresenter
from gawe.api.service import ApiService, Limits
from gawe.api.undo import UndoRuns
from gawe.connectors.credentials import is_read_scope
from gawe.core.anchor import Anchorer, AnchorSink
from gawe.core.approvals import (
    ApprovalRecord,
    ApprovalService,
    ApprovalStore,
    InMemoryApprovalStore,
)
from gawe.core.audit import AuditLog, AuditStore, InMemoryAuditStore
from gawe.core.executor import Executor, RetryPolicy, ToolImplementation
from gawe.core.expiry import Revoker
from gawe.core.guarantees import GUARANTEES, DispatchCondition, required
from gawe.core.history import (
    HISTORY_VARIABLE,
    History,
    HistoryReader,
    HistorySubject,
    InMemoryHistoryReader,
    InMemorySubjectLocks,
    SubjectLocks,
)
from gawe.core.maintenance import InMemoryMaintenanceStore, MaintenanceStore
from gawe.core.models import ToolManifest
from gawe.core.policy.engine import RuleEngine
from gawe.core.policy.provenance import PolicySource, PolicyVariable, validate_provenance
from gawe.core.policy.rules import Rule, load_rules
from gawe.core.policy.signing import PolicySigner
from gawe.core.preparation import ActionVariables, Preparer
from gawe.core.readback import ReadbackAdapter
from gawe.core.records import (
    ExecutionStore,
    InMemoryExecutionStore,
    InMemoryStepStore,
    StepNotFoundError,
    StepStore,
)
from gawe.core.refusals import RefusalObserver
from gawe.core.registry import ToolRegistry
from gawe.core.review import InMemoryReviewStore, ReviewKey, ReviewStore
from gawe.core.runs import InMemoryRunStore, RunNotFoundError, RunRecord, RunStore
from gawe.core.sources import DocumentSource
from gawe.core.telemetry import NoopTelemetry, Telemetry
from gawe.core.undo import InMemoryUndoStore, Undoable, UndoStore
from gawe.core.verification import VerificationResolver
from gawe.core.verifier import PostCondition, Verifier
from gawe.engine.orchestrator import Orchestrator
from gawe.llm.planner import Planner
from gawe.llm.provider import LLMProvider
from gawe.llm.reader import Reader, ReaderProfile
from gawe.maintenance.expiry import GrantExpiryJobs
from gawe.maintenance.readback import ReadbackJobs
from gawe.maintenance.review import DailyReviewJobs, SampleCandidate
from gawe.maintenance.runner import MaintenanceRunner
from gawe.maintenance.settings import MaintenanceSettings


@dataclass(frozen=True)
class AppBundle:
    """What an application contributes."""

    manifests: Sequence[ToolManifest]
    tools: Mapping[str, ToolImplementation]
    policy_rules: Path
    profiles: frozenset[str]
    reader_profile: ReaderProfile | None = None
    variables: Callable[[RunRecord], Mapping[str, Any]] = lambda run: {}
    postconditions: Mapping[str, PostCondition] = field(default_factory=dict)
    policy_sources: Mapping[str, PolicySource] = field(default_factory=dict)
    policy_variables: Mapping[str, PolicyVariable] = field(default_factory=dict)
    preparers: Mapping[tuple[str, str], Preparer] = field(default_factory=dict)
    action_variables: Mapping[tuple[str, str], ActionVariables] = field(default_factory=dict)
    readbacks: Mapping[tuple[str, str], ReadbackAdapter] = field(default_factory=dict)
    refusal_observers: Mapping[tuple[str, str], RefusalObserver] = field(default_factory=dict)
    # (tool, version) -> what its target system guarantees (M22, `gawe.core.guarantees`).
    # Declared by the connectors, not by the manifests; startup checks each write's needs.
    guarantees: Mapping[tuple[str, str], frozenset[str]] = field(default_factory=dict)
    # Tool name -> the scopes of the credential its code is given; declared write resource ->
    # the scope it needs (M23). Startup checks each tool holds what it does and nothing more.
    tool_scopes: Mapping[str, frozenset[str]] = field(default_factory=dict)
    write_scopes: Mapping[str, str] = field(default_factory=dict)
    # (tool, version) -> conditions in other systems, checked just before sending (M22).
    dispatch_conditions: Mapping[tuple[str, str], tuple[DispatchCondition, ...]] = field(
        default_factory=dict
    )
    # (tool, version) writes that grant access until `expires_at`, with the target system's
    # revocation. Each such grant gets an expiry job the sweep runs at its end date.
    revokers: Mapping[tuple[str, str], Revoker] = field(default_factory=dict)
    # Profiles whose runs read their document from a record (a ticket) instead of an upload.
    sources: Mapping[str, DocumentSource] = field(default_factory=dict)
    # Which automatic, executed actions the next-day review may draw for a person to check.
    review_sample: SampleCandidate | None = None
    # (tool, version) -> the role that marks it done. The engine prepares these actions and a
    # person carries them out; nothing ever lets the agent dispatch them (M20). Empty by default.
    prepare_only: Mapping[tuple[str, str], str] = field(default_factory=dict)
    # Tool name -> the argument naming its subject and what its rules read from earlier actions
    # (M24). History rules may only escalate; startup refuses one in an allow rule.
    history: Mapping[str, HistorySubject] = field(default_factory=dict)
    # How long a grant lasts when the app makes one today. A grant made before end dates existed
    # is ended as if made that way, after a notice period (M21). None: the app grants nothing.
    grant_lifetime: timedelta | None = None
    # (tool, version) -> how to read an undo record from its prepared arguments (M26). Each gets
    # one before dispatch; its connector must declare `reversible`. Empty by default.
    undoable: Mapping[tuple[str, str], Undoable] = field(default_factory=dict)


def load_app_rules(bundle: AppBundle) -> tuple[Rule, ...]:
    """Application entry points must validate schemas and provenance before serving runs."""
    unknown_profiles = set(bundle.sources) - set(bundle.profiles)
    if unknown_profiles:
        raise ValueError(f"sources name profiles the app does not have: {sorted(unknown_profiles)}")
    if bundle.sources and bundle.reader_profile is None:
        raise ValueError("a profile with a source needs a Reader to read its document")
    required = {(m.name, m.version) for m in bundle.manifests if m.preparation_required}
    if required != set(bundle.preparers):
        raise ValueError("preparation adapters must match required tool versions")
    for manifest in bundle.manifests:
        if manifest.record_binding is None:
            continue
        if manifest.side_effect_class == "read" or not manifest.preparation_required:
            raise ValueError(
                f"{manifest.name}@{manifest.version}: record binding needs a write "
                "with trusted preparation"
            )
    expiring = {
        (m.name, m.version)
        for m in bundle.manifests
        if not m.retired
        and m.side_effect_class != "read"
        and "expires_at" in m.input_schema.get("required", ())
    }
    # A retired write of the same tool may have a revoker too: its grants had no end date, and
    # GAWE ends them on one it sets (M21). Nothing else may.
    retired_kin = {
        (m.name, m.version)
        for m in bundle.manifests
        if m.retired
        and m.side_effect_class != "read"
        and any(name == m.name for name, _ in expiring)
    }
    unexpected = set(bundle.revokers) - expiring - retired_kin
    if not expiring <= set(bundle.revokers) or unexpected:
        raise ValueError(
            "every write with an end date needs a revoker, and only those: "
            f"missing {sorted(expiring - set(bundle.revokers))}, "
            f"unexpected {sorted(unexpected)}"
        )
    # Retired versions never reach policy: their undispatched steps fail closed.
    rules = load_rules(
        bundle.policy_rules, manifests=[m for m in bundle.manifests if not m.retired]
    )
    validate_provenance(rules, bundle.manifests, bundle.policy_sources, bundle.policy_variables)
    _check_prepare_only(bundle, rules)
    _check_guarantees(bundle, rules)
    _check_undoable(bundle)
    _check_credentials(bundle)
    _check_history(bundle)
    writes = {(m.name, m.version) for m in bundle.manifests if m.side_effect_class != "read"}
    if writes != set(bundle.readbacks):
        raise ValueError("read-back adapters must match write tool versions")
    return rules


def _check_prepare_only(bundle: AppBundle, rules: Sequence[Rule]) -> None:
    """Every prepare-only class names a registered, current write and a role something knows."""
    manifests = {(m.name, m.version): m for m in bundle.manifests}
    known_roles = {r.approver_role for r in rules if r.approver_role} | {
        role for m in bundle.manifests for role in m.required_roles
    }
    for (name, version), role in bundle.prepare_only.items():
        manifest = manifests.get((name, version))
        if manifest is None or manifest.retired:
            raise ValueError(f"prepare_only names {name}@{version}, which the app doesn't register")
        if manifest.side_effect_class == "read":
            raise ValueError(f"prepare_only names {name}@{version}, a read: nothing to hand over")
        if role not in known_roles:
            raise ValueError(f"prepare_only gives {name}@{version} the unknown role {role!r}")


def _check_undoable(bundle: AppBundle) -> None:
    """Every undoable entry names a current write whose target declares it `reversible` (M26).
    A deployment may still make it prepare-only: GAWE then never sends it, so it gets no undo
    record (`build_engine` leaves it out)."""
    manifests = {(m.name, m.version): m for m in bundle.manifests}
    for name, version in bundle.undoable:
        manifest = manifests.get((name, version))
        if manifest is None or manifest.retired or manifest.side_effect_class == "read":
            raise ValueError(f"undoable names {name}@{version}, which isn't a current write")
        if "reversible" not in bundle.guarantees.get((name, version), frozenset()):
            raise ValueError(f"undoable {name}@{version}: its target doesn't declare reversible")


_CONDITION = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _check_guarantees(bundle: AppBundle, rules: Sequence[Rule]) -> None:
    """Every current write's target guarantees what the write relies on, or GAWE never sends it
    (prepare-only). A declaration names only registered tool versions and known guarantees."""
    manifests = {(m.name, m.version): m for m in bundle.manifests}
    for (name, version), declared in bundle.guarantees.items():
        if (name, version) not in manifests:
            raise ValueError(f"guarantees name {name}@{version}, which the app doesn't register")
        unknown = set(declared) - GUARANTEES
        if unknown:
            raise ValueError(f"{name}@{version}: unknown guarantees {sorted(unknown)}")
    for (name, version), conditions in bundle.dispatch_conditions.items():
        manifest = manifests.get((name, version))
        if manifest is None or manifest.retired or manifest.side_effect_class == "read":
            raise ValueError(f"dispatch conditions name {name}@{version}, not a current write")
        names = [c.name for c in conditions]
        if (
            not names
            or len(set(names)) != len(names)
            or not all(_CONDITION.match(n) for n in names)
        ):
            raise ValueError(f"{name}@{version}: dispatch conditions need distinct short names")
    for identity, manifest in manifests.items():
        if identity in bundle.prepare_only:
            continue  # a person carries it out; GAWE never sends it
        missing = required(manifest, rules, bundle.policy_sources, bundle.policy_variables) - set(
            bundle.guarantees.get(identity, frozenset())
        )
        if missing:
            raise ValueError(
                f"{manifest.name}@{manifest.version} relies on {sorted(missing)}, which its "
                "target doesn't guarantee; declare it, or make the action prepare-only"
            )


def _check_credentials(bundle: AppBundle) -> None:
    """Each current tool's credential covers what it does and no change it doesn't make (M23)."""
    for m in bundle.manifests:
        if m.retired:
            continue
        identity = f"{m.name}@{m.version}"
        scopes = bundle.tool_scopes.get(m.name)
        if scopes is None:
            raise ValueError(f"{identity} has no credential (tool_scopes)")
        changes = {s for s in scopes if not is_read_scope(s)}
        if m.side_effect_class == "read":
            if changes:
                raise ValueError(
                    f"{identity} is a read; its credential can change {sorted(changes)}"
                )
            continue
        unknown = [r for r in (m.writes or ()) if r not in bundle.write_scopes]
        if unknown:
            raise ValueError(f"{identity} writes {unknown}, which no scope names (write_scopes)")
        needed = {bundle.write_scopes[r] for r in (m.writes or ())}
        if not needed <= scopes:
            raise ValueError(f"{identity}'s credential lacks {sorted(needed - scopes)}")
        if changes - needed:
            raise ValueError(f"{identity}'s credential can also change {sorted(changes - needed)}")


def _check_history(bundle: AppBundle) -> None:
    """History is declared whole (M24): each subject names a tool and an argument it has; each
    history source is `gawe.history.<tool>` of a declared tool; and each `history.*` variable
    reads a history source, so the allow-rule refusal in provenance sees it, plus at most tenant
    settings."""
    current = [m for m in bundle.manifests if not m.retired]
    for tool, subject in bundle.history.items():
        if subject.tools:
            family = set(subject.tools)
            if len(family) != len(subject.tools) or tool not in family:
                raise ValueError("history family must include its tool exactly once")
            for member in family:
                peer = bundle.history.get(member)
                if (
                    peer is None
                    or set(peer.tools) != family
                    or peer.argument != subject.argument
                    or peer.lower != subject.lower
                ):
                    raise ValueError("history family must share members and subject identity")
        versions = [m for m in current if m.name == tool]
        if not versions:
            raise ValueError(f"history declared for unknown tool {tool}")
        for m in versions:
            if subject.argument not in m.input_schema.get("properties", {}):
                raise ValueError(
                    f"{m.name}@{m.version} has no argument {subject.argument!r} to key history on"
                )
            for name in subject.shown:
                if (m.field_classes or {}).get(name) != "typed":
                    raise ValueError(
                        f"{m.name}@{m.version}: history shows {name!r}, which isn't a typed field"
                    )
    history_sources = set()
    for name, source in bundle.policy_sources.items():
        if source.kind == "history":
            tool = source.resource.removeprefix("gawe.history.")
            if not source.resource.startswith("gawe.history.") or tool not in bundle.history:
                raise ValueError(f"history source {name} names no declared history")
            history_sources.add(name)
        elif source.resource.startswith("gawe.history."):
            raise ValueError(f"source {name} reads history but isn't of kind history")
    # Tenant settings may shape a history variable (a window, a count); findings and target
    # resources may not, so what an action did is the only input a document could not choose.
    settings = {n for n, src in bundle.policy_sources.items() if src.kind == "configuration"}
    for name, variable in bundle.policy_variables.items():
        deps = set(variable.dependencies)
        if name.split(".")[0] == HISTORY_VARIABLE and not (
            deps & history_sources and deps <= history_sources | settings
        ):
            raise ValueError(
                f"variable {name} must read a history source, and otherwise settings only"
            )


@dataclass(frozen=True)
class Stores:
    runs: RunStore
    steps: StepStore
    executions: ExecutionStore
    approvals: ApprovalStore
    audit: AuditStore
    maintenance: MaintenanceStore = field(default_factory=InMemoryMaintenanceStore)
    reviews: ReviewStore = field(default_factory=InMemoryReviewStore)
    # M24: an app's own history, read from the run and step stores above, and the subject locks.
    history: HistoryReader | None = None
    locks: SubjectLocks = field(default_factory=InMemorySubjectLocks)
    # M26: undo records, written before each undoable write.
    undo: UndoStore = field(default_factory=InMemoryUndoStore)


def memory_stores() -> Stores:
    runs, steps = InMemoryRunStore(), InMemoryStepStore()

    def describe(record: ApprovalRecord) -> tuple[str, str, str] | None:
        try:
            run = runs.get(record.run_id)
            step = steps.get(record.run_id, record.step_id)
        except (RunNotFoundError, StepNotFoundError):
            return None
        return run.tenant_id, step.tool, step.tool_version

    return Stores(
        runs=runs,
        steps=steps,
        executions=InMemoryExecutionStore(),
        approvals=InMemoryApprovalStore(describe),
        audit=InMemoryAuditStore(),
        history=InMemoryHistoryReader(runs, steps),
    )


def postgres_stores(engine: Any) -> Stores:
    """Every call commits on its own, which the executor's crash-recovery relies on."""
    from gawe.db.approval_store import PostgresApprovalStore
    from gawe.db.audit_store import PostgresAuditStore
    from gawe.db.execution_store import PostgresExecutionStore
    from gawe.db.history_store import PostgresHistoryReader, PostgresSubjectLocks
    from gawe.db.maintenance_store import PostgresMaintenanceStore
    from gawe.db.review_store import PostgresReviewStore
    from gawe.db.run_store import PostgresRunStore
    from gawe.db.step_store import PostgresStepStore
    from gawe.db.undo_store import PostgresUndoStore

    return Stores(
        runs=PostgresRunStore(engine),
        steps=PostgresStepStore(engine),
        executions=PostgresExecutionStore(engine),
        approvals=PostgresApprovalStore(engine),
        audit=PostgresAuditStore(engine),
        maintenance=PostgresMaintenanceStore(engine),
        reviews=PostgresReviewStore(engine),
        history=PostgresHistoryReader(engine),
        locks=PostgresSubjectLocks(engine),
        undo=PostgresUndoStore(engine),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Engine:
    """The engine parts shared by the API and the standalone maintenance command."""

    registry: ToolRegistry
    audit: AuditLog
    anchorer: Anchorer
    approvals: ApprovalService
    verification: VerificationResolver
    orchestrator: Orchestrator
    runner: MaintenanceRunner
    undo: UndoRuns


def build_engine(
    bundle: AppBundle,
    *,
    stores: Stores,
    signer: PolicySigner,
    provider: LLMProvider | None,
    telemetry: Telemetry | None = None,
    retry: RetryPolicy | None = None,
    anchor_sinks: Sequence[AnchorSink] = (),
    maintenance: MaintenanceSettings | None = None,
    continuation: Callable[[str], object] | None = None,
    review_key: ReviewKey | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> Engine:
    """Without `provider` the orchestrator cannot plan or read; it only drives existing plans.

    `continuation` drives a run after the sweep resolves its verification; by default the sweep
    drives it inline. The API passes its background submission instead.
    """
    settings = maintenance or MaintenanceSettings()
    registry = ToolRegistry()
    for manifest in bundle.manifests:
        registry.register(manifest)
    missing = {m.name for m in bundle.manifests} - set(bundle.tools)
    if missing:
        raise ValueError(f"no implementation for tools: {sorted(missing)}")

    rules = load_app_rules(bundle)
    telemetry = telemetry or NoopTelemetry()
    audit = AuditLog(stores.audit, clock=clock)
    anchorer = Anchorer(audit, anchor_sinks, telemetry=telemetry, clock=clock)
    approvals = ApprovalService(stores.approvals, signer, audit, clock=clock)
    verification = VerificationResolver(
        jobs=stores.maintenance,
        steps=stores.steps,
        executions=stores.executions,
        runs=stores.runs,
        registry=registry,
        adapters=bundle.readbacks,
        refusal_observers=bundle.refusal_observers,
        audit=audit,
        clock=clock,
        window=settings.deadline,
        # Expiry jobs at dispatch only for current versions; a retired one's revoker is there
        # for grants GAWE ends on a date it sets (M21).
        expiring=frozenset(
            k
            for k in bundle.revokers
            if k in {(m.name, m.version) for m in bundle.manifests if not m.retired}
        ),
        undoable={k: v for k, v in bundle.undoable.items() if k not in bundle.prepare_only},
        undo=stores.undo,
    )
    executor = Executor(
        steps=stores.steps,
        executions=stores.executions,
        registry=registry,
        tools=bundle.tools,
        signer=signer,
        approvals=approvals,
        audit=audit,
        verifier=Verifier(bundle.postconditions),
        verification=verification,
        retry=retry,
        clock=clock,
        prepare_only=frozenset(bundle.prepare_only),
        conditions=bundle.dispatch_conditions,
    )
    policy = RuleEngine(rules, signer)
    if bundle.history and stores.history is None:
        raise ValueError("the app reads history but the stores have no history reader")
    history = (
        History(bundle.history, stores.history, stores.locks)
        if bundle.history and stores.history is not None
        else None
    )
    undo_handlers: list[UndoRuns] = []

    def finished(run: RunRecord) -> None:
        for handler in undo_handlers:
            handler.finished(run)
        if anchor_sinks:
            anchorer.anchor_run(run)

    orchestrator = Orchestrator(
        runs=stores.runs,
        steps=stores.steps,
        executions=stores.executions,
        registry=registry,
        policy=policy,
        executor=executor,
        approvals=approvals,
        planner=Planner(provider, registry) if provider is not None else None,
        reader=(
            Reader(provider, bundle.reader_profile)
            if provider is not None and bundle.reader_profile
            else None
        ),
        audit=audit,
        telemetry=telemetry,
        variables=bundle.variables,
        preparers=bundle.preparers,
        action_variables=bundle.action_variables,
        on_finished=finished,
        clock=clock,
        prepare_only=bundle.prepare_only,
        history=history,
    )
    undo = UndoRuns(verification, orchestrator, stores.locks, telemetry)
    undo_handlers.append(undo)
    runner = MaintenanceRunner(
        store=stores.maintenance,
        handlers=[
            *([undo] if bundle.undoable else []),
            ReadbackJobs(verification, continuation or orchestrator.advance),
            *(
                [
                    GrantExpiryJobs(
                        revokers=bundle.revokers,
                        jobs=stores.maintenance,
                        runs=stores.runs,
                        audit=audit,
                        anchorer=anchorer,
                        clock=clock,
                    )
                ]
                if bundle.revokers
                else []
            ),
            DailyReviewJobs(
                jobs=stores.maintenance,
                runs=stores.runs,
                steps=stores.steps,
                audit_store=stores.audit,
                audit=audit,
                reviews=stores.reviews,
                registry=registry,
                engine=policy,
                signer=signer,
                variables=bundle.variables,
                history=history,
                anchorer=anchorer,
                clock=clock,
                key=review_key,
                sample=bundle.review_sample,
                telemetry=telemetry,
            ),
        ],
        telemetry=telemetry,
        clock=clock,
        batch_size=settings.batch_size,
        max_jobs=settings.max_jobs,
        retry_after=settings.interval,
    )
    return Engine(registry, audit, anchorer, approvals, verification, orchestrator, runner, undo)


def assemble(
    bundle: AppBundle,
    *,
    stores: Stores,
    signer: PolicySigner,
    provider: LLMProvider,
    background: Background,
    telemetry: Telemetry | None = None,
    limits: Limits | None = None,
    retry: RetryPolicy | None = None,
    anchor_sinks: Sequence[AnchorSink] = (),
    maintenance: MaintenanceSettings | None = None,
    review_key: ReviewKey | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> ApiService:
    """With `anchor_sinks`, every run's audit head is anchored when the run ends."""
    services: list[ApiService] = []

    def continue_in_background(run_id: str) -> None:
        # Same path as resume: off the sweep thread, with advance errors recorded in the audit.
        services[0].kick(run_id)

    engine = build_engine(
        bundle,
        stores=stores,
        signer=signer,
        provider=provider,
        telemetry=telemetry,
        retry=retry,
        anchor_sinks=anchor_sinks,
        maintenance=maintenance,
        continuation=continue_in_background,
        review_key=review_key,
        clock=clock,
    )
    service = ApiService(
        orchestrator=engine.orchestrator,
        runs=stores.runs,
        steps=stores.steps,
        approval_service=engine.approvals,
        approval_store=stores.approvals,
        registry=engine.registry,
        audit=engine.audit,
        background=background,
        profiles=bundle.profiles,
        undo=engine.undo,
        record_presenter=RecordPresenter(engine.registry, bundle.refusal_observers, stores.undo),
        limits=limits,
        anchorer=engine.anchorer,
        maintenance=stores.maintenance,
        maintenance_runner=engine.runner,
        reviews=stores.reviews,
        sources=bundle.sources,
        checked_not_enforced={
            k: tuple(c.name for c in v) for k, v in bundle.dispatch_conditions.items()
        },
        executions=stores.executions,
        signer=signer,
        readback_tools=frozenset(bundle.readbacks),
        clock=clock,
    )
    services.append(service)
    return service
