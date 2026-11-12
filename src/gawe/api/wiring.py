"""Assembles the engine and the API service from parts.

An application (Contract Review, the IT-access fixture) supplies an `AppBundle`: its tools, their
manifests, its policy rules and its Reader profile. Everything else is common. Stores come in
two kinds: in-memory (local demos and tests) and Postgres (everything survives a restart).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gawe.api.background import Background
from gawe.api.service import ApiService, Limits
from gawe.core.anchor import Anchorer, AnchorSink
from gawe.core.approvals import ApprovalService, ApprovalStore, InMemoryApprovalStore
from gawe.core.audit import AuditLog, AuditStore, InMemoryAuditStore
from gawe.core.executor import Executor, RetryPolicy, ToolImplementation
from gawe.core.models import ToolManifest
from gawe.core.policy.engine import RuleEngine
from gawe.core.policy.rules import load_rules
from gawe.core.policy.signing import PolicySigner
from gawe.core.records import (
    ExecutionStore,
    InMemoryExecutionStore,
    InMemoryStepStore,
    StepStore,
)
from gawe.core.registry import ToolRegistry
from gawe.core.runs import InMemoryRunStore, RunRecord, RunStore
from gawe.core.telemetry import NoopTelemetry, Telemetry
from gawe.core.verifier import PostCondition, Verifier
from gawe.engine.orchestrator import Orchestrator
from gawe.llm.planner import Planner
from gawe.llm.provider import LLMProvider
from gawe.llm.reader import Reader, ReaderProfile


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


@dataclass(frozen=True)
class Stores:
    runs: RunStore
    steps: StepStore
    executions: ExecutionStore
    approvals: ApprovalStore
    audit: AuditStore


def memory_stores() -> Stores:
    return Stores(
        runs=InMemoryRunStore(),
        steps=InMemoryStepStore(),
        executions=InMemoryExecutionStore(),
        approvals=InMemoryApprovalStore(),
        audit=InMemoryAuditStore(),
    )


def postgres_stores(engine: Any) -> Stores:
    """Every call commits on its own, which the executor's crash-recovery relies on."""
    from gawe.db.approval_store import PostgresApprovalStore
    from gawe.db.audit_store import PostgresAuditStore
    from gawe.db.execution_store import PostgresExecutionStore
    from gawe.db.run_store import PostgresRunStore
    from gawe.db.step_store import PostgresStepStore

    return Stores(
        runs=PostgresRunStore(engine),
        steps=PostgresStepStore(engine),
        executions=PostgresExecutionStore(engine),
        approvals=PostgresApprovalStore(engine),
        audit=PostgresAuditStore(engine),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    clock: Callable[[], datetime] = _utc_now,
) -> ApiService:
    """With `anchor_sinks`, every run's audit head is anchored when the run ends."""
    registry = ToolRegistry()
    for manifest in bundle.manifests:
        registry.register(manifest)
    missing = {m.name for m in bundle.manifests} - set(bundle.tools)
    if missing:
        raise ValueError(f"no implementation for tools: {sorted(missing)}")

    audit = AuditLog(stores.audit, clock=clock)
    anchorer = Anchorer(audit, anchor_sinks, telemetry=telemetry, clock=clock)
    approvals = ApprovalService(stores.approvals, signer, audit, clock=clock)
    executor = Executor(
        steps=stores.steps,
        executions=stores.executions,
        registry=registry,
        tools=bundle.tools,
        signer=signer,
        approvals=approvals,
        audit=audit,
        verifier=Verifier(bundle.postconditions),
        retry=retry,
        clock=clock,
    )
    orchestrator = Orchestrator(
        runs=stores.runs,
        steps=stores.steps,
        executions=stores.executions,
        registry=registry,
        policy=RuleEngine(load_rules(bundle.policy_rules, manifests=bundle.manifests), signer),
        executor=executor,
        approvals=approvals,
        planner=Planner(provider, registry),
        reader=Reader(provider, bundle.reader_profile) if bundle.reader_profile else None,
        audit=audit,
        telemetry=telemetry or NoopTelemetry(),
        variables=bundle.variables,
        on_finished=anchorer.anchor_run if anchor_sinks else None,
        clock=clock,
    )
    return ApiService(
        orchestrator=orchestrator,
        runs=stores.runs,
        steps=stores.steps,
        approval_service=approvals,
        approval_store=stores.approvals,
        registry=registry,
        audit=audit,
        background=background,
        profiles=bundle.profiles,
        limits=limits,
        anchorer=anchorer,
    )
