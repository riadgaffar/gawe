"""M27 record-change explanations: fixed plans, real engine, local mocks, no model calls.

Each case has an independent literal oracle. Volatile ids, hashes and times are checked through
binding/stability booleans rather than emitted. Projection attacks inject a copied saved view;
they never overwrite immutable evidence or authorize another write.
"""

import secrets
import tempfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from bench.results import ResultsWriter
from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.schemas import RecordEvidenceView
from gawe.api.wiring import assemble, memory_stores
from gawe.apps import contract_review, event_replay, it_access
from gawe.connectors.base import ConnectorError, Ledger
from gawe.connectors.mock_billing import MockBilling
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_iam import MockIAM
from gawe.connectors.mock_tickets import MockTickets
from gawe.core.audit import verify_chain
from gawe.core.canonical import action_hash
from gawe.core.models import Plan, PlanStep
from gawe.core.policy.signing import PolicySigner
from gawe.core.records import StepRecord
from gawe.core.undo import InMemoryUndoStore
from gawe.llm.fake import FakeProvider

KIND = "record_changes"
TENANT = "00000000-0000-0000-0000-00000000aa01"
ACTOR = Identity(
    TENANT, "record-bench", frozenset({"case_manager", "it_agent", "billing_operator"})
)
SECRET = "PRIVATE_RECORD_MARKER"


@dataclass
class Clock:
    now: datetime = datetime(2026, 11, 25, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Crash(BaseException):
    """Simulated process loss, outside ordinary connector error handling."""


@dataclass(frozen=True)
class Case:
    case_id: str
    mode: str
    expected: Mapping[str, Any]


def expected(
    state: str = "changed",
    *,
    timing: str | None = "at_guard",
    before: Any = (45000,),
    after: Any = (150000,),
    coverage: str = "complete",
    subjects: Sequence[str] = ("C-1001",),
    states: Sequence[str] | None = None,
    timings: Sequence[str | None] | None = None,
    captures: Any = None,
    observations: Any = None,
    basis: Sequence[str | None] = ("preparation",),
    error: str = "record_changed",
    tamper_changes_hash: bool | None = None,
) -> dict[str, Any]:
    # Literal expectations only: never consult an app, projector, policy or connector.
    return {
        "run_status": "failed",
        "error": error,
        "effects": 0,
        "attempts": 1,
        "open_approvals": 0,
        "audit_valid": True,
        "refresh_stable": True,
        "saved_evidence_stable": True,
        "private_value_exposed": False,
        "capture_binding_valid": True,
        "action_bound": True,
        "tamper_changes_hash": tamper_changes_hash,
        "subjects": list(subjects),
        "states": list(states) if states is not None else [state],
        "timings": list(timings) if timings is not None else [timing],
        "coverage": coverage,
        "captured": captures
        if captures is not None
        else [list(before) if before is not None else None],
        "observed": observations
        if observations is not None
        else [list(after) if after is not None else None],
        "basis": list(basis),
    }


REPLAY_BEFORE = [["cus-northwind", 120], ["cus-northwind", 340]]
CASES = (
    Case("RC01-selected-field-change", "changed", expected()),
    Case("RC02-away-and-back-fields-match", "unchanged", expected("unchanged", after=(45000,))),
    Case(
        "RC03-later-read-is-not-guard-state",
        "later",
        expected(timing="after_refusal", after=(250000,)),
    ),
    Case(
        "RC04-record-missing",
        "missing",
        expected("record_missing", timing="after_refusal", after=None),
    ),
    Case(
        "RC05-record-unreadable",
        "unreadable",
        expected("observation_unavailable", timing="after_refusal", after=None),
    ),
    Case(
        "RC06-old-snapshotless-action",
        "legacy",
        expected("snapshot_unavailable", before=None, basis=(None,)),
    ),
    Case(
        "RC07-diagnostic-not-recorded",
        "no_diagnostic",
        expected("observation_unavailable", timing=None, after=None, coverage="unavailable"),
    ),
    Case("RC08-undo-original-provenance", "undo", expected(basis=("confirmed_original",))),
    Case(
        "RC09-replay-partial-guard",
        "replay",
        expected(
            error="event_changed",
            subjects=("ev-1001", "ev-1002"),
            states=("changed", "observation_unavailable"),
            timings=("at_guard", None),
            captures=REPLAY_BEFORE,
            observations=[["cus-northwind", 999], None],
            basis=("preparation", "preparation"),
            coverage="partial",
        ),
    ),
    Case(
        "RC10-replay-target-moved-tenant",
        "tenant",
        expected(
            error="event_changed",
            subjects=("ev-1001", "ev-1002"),
            states=("record_missing", "unchanged"),
            timings=("after_refusal", "after_refusal"),
            captures=REPLAY_BEFORE,
            observations=[None, ["cus-northwind", 340]],
            basis=("preparation", "preparation"),
        ),
    ),
    Case(
        "RC11-snapshot-hash-tampering",
        "snapshot",
        expected(
            subjects=(),
            states=(),
            timings=(),
            captures=[],
            observations=[],
            basis=(),
            coverage="unavailable",
            tamper_changes_hash=True,
        ),
    ),
    Case(
        "RC12-foreign-observation",
        "foreign",
        expected("observation_unavailable", timing=None, after=None, coverage="unavailable"),
    ),
    Case(
        "RC13-diagnostic-action-tampering",
        "digest",
        expected("observation_unavailable", timing=None, after=None, coverage="unavailable"),
    ),
    Case(
        "RC14-undeclared-field",
        "field",
        expected("observation_unavailable", timing=None, after=None, coverage="unavailable"),
    ),
    Case(
        "RC15-false-complete-batch",
        "coverage",
        expected(
            error="event_changed",
            subjects=("ev-1001", "ev-1002"),
            states=("observation_unavailable", "observation_unavailable"),
            timings=(None, None),
            captures=REPLAY_BEFORE,
            observations=[None, None],
            basis=("preparation", "preparation"),
            coverage="unavailable",
        ),
    ),
    Case("RC16-saved-proof-crash", "crash", expected()),
    Case(
        "RC17-iam-no-fields-tracked",
        "iam",
        expected("no_fields_tracked", subjects=("dana.ortiz@acme.example",), before=(), after=()),
    ),
    Case(
        "RC18-undo-source-unavailable",
        "undo_missing",
        expected("snapshot_unavailable", before=None, basis=(None,)),
    ),
)


def observe(case: Case, directory: Path) -> dict[str, Any]:
    mode, clock, stores = case.mode, Clock(), memory_stores()
    ledger = Ledger(directory / "ledger.jsonl")
    owner: Any
    args: dict[str, Any]
    source_id = None
    findings = None
    if mode in {"replay", "tenant", "coverage"}:
        owner = MockBilling.from_seed(
            ledger, daily_limits={}, default_daily_limit=event_replay.DAILY_REPLAY_LIMIT
        )
        bundle = event_replay.bundle(directory, billing=owner)
        tool, args = "billing.replay_events", {"event_ids": ["ev-1001", "ev-1002"]}
    elif mode == "iam":
        owner = MockIAM.from_seed(it_access.HERE / "samples/iam_seed.json", ledger)
        tickets = MockTickets.in_state_dir(directory)
        bundle = it_access.bundle(directory, iam=owner, tickets=tickets, clock=clock)
        source_id = "record-bench-ticket"
        tickets.create(
            TENANT,
            source_id,
            submitted_by="dana.ortiz@acme.example",
            channel="portal",
            body="Read wiki",
        )
        findings = {"requester_email": "dana.ortiz@acme.example"}
        tool, args = (
            "iam.grant_access",
            {"email": "dana.ortiz@acme.example", "system": "wiki", "level": "read"},
        )
    else:
        owner = MockCLM.from_seed(contract_review.HERE / "samples/clm_seed.json", ledger)
        bundle = contract_review.bundle(directory, clm=owner)
        tool, args = (
            "crm.write_assessment",
            {"contract_id": "C-1001", "risk_level": "high", "summary": "new"},
        )
    signer = PolicySigner(secrets.token_bytes(32))

    def assemble_service() -> Any:
        return assemble(
            bundle,
            stores=stores,
            signer=signer,
            provider=FakeProvider([]),
            background=InlineBackground(),
            clock=clock,
        )

    service = assemble_service()
    run = service._orch.start_run(
        tenant_id=TENANT,
        acting_user_id=ACTOR.user_id,
        roles=tuple(ACTOR.roles),
        objective="Record change benchmark",
        profile=next(iter(bundle.profiles)),
        source_id=source_id,
    )
    stores.runs.update(
        run.run_id,
        plan=Plan(steps=(PlanStep(id="write", tool=tool, args=args),)),
        plan_version=1,
        findings=findings,
    )
    if mode == "legacy":
        stores.steps.add(
            StepRecord(
                run_id=run.run_id,
                step_id="write",
                tool=tool,
                tool_version="3.0.0",
                args_template=args,
            )
        )
    source = None
    if mode in {"undo", "undo_missing"}:
        owner.set_assessment(
            "seed", "C-1001", "high", "before", contract_version=owner.contract_version("C-1001")
        )
        service._orch.advance(run.run_id)
        source = stores.undo.for_run(TENANT, run.run_id)[0]
        tool = "crm.restore_assessment"
    original = service._orch._executor._tools[tool]
    effects_before = len(owner.operations.records())

    def attacked(values: Mapping[str, Any], call: Any) -> Any:
        if mode == "iam":
            owner.replace_user("dana.ortiz@acme.example", {"department": SECRET})
        elif mode in {"replay", "coverage"}:
            owner.change_event("ev-1001", {"amount": 999})
        elif mode == "tenant":
            owner.change_event("ev-1001", {"tenant_id": "other", "customer": SECRET})
            raise ConnectorError("event_changed")
        elif mode in {"later", "missing", "unreadable"}:
            owner.replace_contract("C-1001", {"deal_value": 250000})
            if mode != "later":

                def unavailable(_: str) -> Any:
                    if mode == "missing":
                        raise ConnectorError("contract_not_found")
                    raise OSError(SECRET)

                owner.current_contract = unavailable
            raise ConnectorError("record_changed")
        elif mode == "unchanged":
            owner.replace_contract("C-1001", {"deal_value": 150000, "note": SECRET})
            owner.replace_contract("C-1001", {"deal_value": 45000})
        else:
            owner.replace_contract("C-1001", {"deal_value": 150000, "note": SECRET})
        return original(values, call)

    service._orch._executor._tools[tool] = attacked
    save = stores.maintenance.replace
    if mode == "crash":

        def crash_after_proof(job: Any, **kwargs: Any) -> Any:
            saved = save(job, **kwargs)
            if "refusal" in saved.evidence and saved.result is None:
                raise Crash()
            return saved

        stores.maintenance.replace = crash_after_proof  # type: ignore[method-assign]
    if source is not None:
        run_id = service.request_undo(ACTOR, run.run_id, "write").run_id
        step_id = "restore"
    else:
        run_id, step_id = run.run_id, "write"
        try:
            service._orch.advance(run_id)
        except Crash:
            stores.maintenance.replace = save  # type: ignore[method-assign]
            owner.replace_contract("C-1001", {"deal_value": 250000})
            clock.now += timedelta(minutes=6)
            service = assemble_service()
            service.recover_interrupted()
            service._orch.advance(run_id)
    step = stores.steps.get(run_id, step_id)
    verification = service.verification_evidence(ACTOR, run_id)[0]
    saved_evidence = deepcopy(verification.evidence)
    presented_step = step
    changed_hash = None
    evidence = deepcopy(verification.evidence)
    if mode == "no_diagnostic":
        evidence.pop("diagnostic", None)
    elif mode == "foreign":
        evidence["diagnostic"]["observations"][0]["subject"] = SECRET
    elif mode == "digest":
        evidence["diagnostic"]["action_hash"] = "a" * 64
    elif mode == "field":
        evidence["diagnostic"]["observations"][0]["fields"].append(
            {"path": "private", "present": True, "value": SECRET}
        )
    elif mode == "coverage":
        evidence["diagnostic"]["coverage"] = "complete"
    elif mode == "snapshot":
        changed = deepcopy(step.resolved_args)
        changed["snapshots"][0]["fields"][0]["value"] = 1
        changed_hash = (
            action_hash(
                tool=step.tool,
                version=step.tool_version,
                resolved_args=changed,
                run_id=run_id,
                step_id=step_id,
            )
            != step.action_hash
        )
        presented_step = step.model_copy(update={"resolved_args": changed})
    elif mode == "undo_missing":
        service._record_presenter.undo = InMemoryUndoStore()
    verification = verification.model_copy(update={"evidence": evidence})

    def project() -> RecordEvidenceView:
        shown = service._record_presenter.project(
            stores.runs.get(run_id), presented_step, verification=verification
        )
        assert isinstance(shown, RecordEvidenceView)
        return shown

    shown = project()
    # Another edit after the saved observation cannot silently replace it on refresh.
    if mode not in {"iam", "replay", "coverage", "tenant"}:
        owner.replace_contract("C-1001", {"deal_value": 999000})
    binding_valid = True
    for target in shown.records:
        snapshot = target.snapshot
        if snapshot is not None:
            if source is not None:
                binding_valid &= (
                    snapshot.source_action_hash == source.action_hash
                    and snapshot.revision == source.bound_version
                    and target.guard_revision == source.result_version
                    and snapshot.fields == source.snapshots[0].fields
                )
            else:
                binding_valid &= (
                    snapshot.source_action_hash is None
                    and snapshot.revision == target.guard_revision
                )
    return {
        "case": case.case_id,
        "run_status": stores.runs.get(run_id).status.value,
        "error": step.error,
        "effects": len(owner.operations.records()) - effects_before,
        "attempts": len(stores.executions.attempts(run_id, step_id)),
        "open_approvals": len(list(stores.approvals.open_approvals())),
        "audit_valid": all(
            verify_chain(stores.audit.events(r.run_id)).ok
            for r in stores.runs.list_for_tenant(TENANT)
        ),
        "refresh_stable": project() == shown,
        "saved_evidence_stable": service.verification_evidence(ACTOR, run_id)[0].evidence
        == saved_evidence,
        "private_value_exposed": SECRET in shown.model_dump_json(),
        "capture_binding_valid": bool(binding_valid),
        "action_bound": shown.action_hash == step.action_hash,
        "tamper_changes_hash": changed_hash,
        "subjects": [r.subject for r in shown.records],
        "states": [r.state for r in shown.records],
        "timings": [r.observation.timing if r.observation else None for r in shown.records],
        "coverage": shown.coverage,
        "captured": [
            [f.value for f in r.snapshot.fields] if r.snapshot else None for r in shown.records
        ],
        "observed": [
            [f.value for f in r.observation.fields]
            if r.observation and r.observation.status == "observed"
            else None
            for r in shown.records
        ],
        "basis": [r.snapshot.basis if r.snapshot else None for r in shown.records],
    }


def expected_record(case: Case) -> dict[str, Any]:
    return {"case": case.case_id, **case.expected}


def differences(actual: Any, wanted: Any, path: str = "$") -> dict[str, Any]:
    """Exact values, scalar types, key sets and array order, without normalization."""
    if type(actual) is not type(wanted):
        return {path: {"expected": wanted, "actual": actual, "reason": "type"}}
    if isinstance(actual, dict):
        if actual.keys() != wanted.keys():
            return {path: {"expected_keys": sorted(wanted), "actual_keys": sorted(actual)}}
        return {
            k: v
            for key in actual
            for k, v in differences(actual[key], wanted[key], f"{path}.{key}").items()
        }
    if isinstance(actual, list):
        if len(actual) != len(wanted):
            return {path: {"expected": wanted, "actual": actual, "reason": "length"}}
        return {
            k: v
            for i, (a, b) in enumerate(zip(actual, wanted, strict=True))
            for k, v in differences(a, b, f"{path}[{i}]").items()
        }
    return {} if actual == wanted else {path: {"expected": wanted, "actual": actual}}


def run_suite(
    results_dir: Path,
    *,
    stamp: str,
    git: tuple[str, bool],
    out: TextIO,
    cases: Sequence[Case] = CASES,
) -> bool:
    writer = ResultsWriter(results_dir / f"{stamp}-record-changes")
    writer.write_manifest(
        {
            "schema": "gawe.bench.record_changes.v1",
            "kind": KIND,
            "bench_run_id": f"{stamp}-record-changes",
            "git_commit": git[0],
            "git_dirty": git[1],
            "model": None,
            "cases": len(cases),
        }
    )
    mismatched = 0
    for case in cases:
        with tempfile.TemporaryDirectory(prefix="gawe-record-changes-") as directory:
            actual = observe(case, Path(directory))
        diff = differences(actual, expected_record(case))
        writer.append(KIND, {**actual, "matches_expected": not diff, "differences": diff})
        mismatched += bool(diff)
        print(f"{'MISMATCH' if diff else 'ok':8} {case.case_id} {diff or ''}", file=out)
    writer.finish(
        complete=True, reason=None, spent_usd=Decimal(0), extra={"mismatched": mismatched}
    )
    print(
        f"record changes: {len(cases) - mismatched}/{len(cases)} match their expected records",
        file=out,
    )
    print(f"wrote {writer.directory}", file=out)
    return mismatched == 0
