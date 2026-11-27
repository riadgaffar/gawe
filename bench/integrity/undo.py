"""M26 undo integrity: a separate four-case group, never part of the original 23 or 38.

Real preparation, policy, dispatch and independent read-back run against the mock contract
system. A fake Planner proposes the original write; undo cannot call a model. Race outcomes
normalize only which contender wins: acceptance counts and the loser's error remain checked.
"""

import secrets
import tempfile
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from bench.integrity.scenarios import differences
from bench.results import ResultsWriter
from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.undo import UndoRefused
from gawe.api.wiring import assemble, memory_stores
from gawe.apps import contract_review
from gawe.connectors.base import ConnectorError, Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.core.audit import verify_chain
from gawe.core.policy.signing import PolicySigner
from gawe.core.undo import reversal_key
from gawe.llm.fake import FakeProvider

REQUESTER = Identity("undo-bench", "requester", frozenset({"case_manager"}))
CONTRACT = "C-1001"
KIND = "undo_integrity"


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    expected: Mapping[str, Any]


# Hand-written expectations: no oracle calls the policy, connector or undo implementation.
SCENARIOS = (
    Scenario(
        "U01-away-and-back",
        {
            "original_status": "completed",
            "undo_statuses": ["failed"],
            "errors": ["record_changed"],
            "new_runs": 1,
            "accepted_restores": 0,
            "accepted_competitors": 0,
            "final_rating_matches_winner": True,
            "undo_states": ["failed"],
            "audit_valid": True,
        },
    ),
    Scenario(
        "U02-unknown-original",
        {
            "original_status": "unknown",
            "undo_statuses": [],
            "errors": ["undo_unavailable"],
            "new_runs": 0,
            "accepted_restores": 0,
            "accepted_competitors": 0,
            "final_rating_matches_winner": True,
            "undo_states": ["unavailable"],
            "audit_valid": True,
        },
    ),
    Scenario(
        "U03-double-undo",
        {
            "original_status": "completed",
            "undo_statuses": ["completed"],
            "errors": ["already_undone"],
            "new_runs": 1,
            "accepted_restores": 1,
            "accepted_competitors": 0,
            "final_rating_matches_winner": True,
            "undo_states": ["used"],
            "audit_valid": True,
        },
    ),
    Scenario(
        "U04-racing-write",
        {
            "original_status": "completed",
            "undo_statuses": ["one_winner"],
            "errors": ["record_changed"],
            "new_runs": 1,
            "accepted_restores": "zero_or_one",
            "accepted_competitors": "complement",
            "final_rating_matches_winner": True,
            "undo_states": ["consistent_with_winner"],
            "audit_valid": True,
        },
    ),
)


@dataclass
class Clock:
    now: datetime = datetime(2026, 11, 25, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def observe(scenario: Scenario, directory: Path) -> dict[str, Any]:
    clock = Clock()
    stores = memory_stores()
    clm = MockCLM.from_seed(
        contract_review.HERE / "samples/clm_seed.json", Ledger(directory / "ledger.jsonl")
    )
    clm.set_assessment(
        "seed", CONTRACT, "high", "before", contract_version=clm.contract_version(CONTRACT)
    )
    service = assemble(
        contract_review.bundle(directory, clm=clm),
        stores=stores,
        signer=PolicySigner(secrets.token_bytes(32)),
        provider=FakeProvider(
            [
                {
                    "steps": [
                        {
                            "id": "write",
                            "tool": "crm.write_assessment",
                            "args": {
                                "contract_id": CONTRACT,
                                "risk_level": "high",
                                "summary": "new",
                            },
                        }
                    ]
                }
            ]
        ),
        background=InlineBackground(),
        clock=clock,
    )
    run = service._orch.start_run(
        tenant_id=REQUESTER.tenant_id,
        acting_user_id=REQUESTER.user_id,
        roles=tuple(REQUESTER.roles),
        objective="rate",
        profile="contract_review",
    )
    if scenario.scenario_id == "U02-unknown-original":
        clm.operations.visible_at = clock.now + timedelta(minutes=10)
    service._orch.advance(run.run_id)
    if scenario.scenario_id == "U02-unknown-original":
        clock.now += timedelta(minutes=5)
        service._orch.advance(run.run_id)
    original_status = stores.runs.get(run.run_id).status.value
    record = stores.undo.for_run(REQUESTER.tenant_id, run.run_id)[0]
    statuses: list[str] = []
    errors: list[str] = []

    def request() -> tuple[str | None, str | None]:
        try:
            undone = service.request_undo(REQUESTER, run.run_id, "write")
            return undone.status, stores.steps.get(undone.run_id, "restore").error
        except UndoRefused as exc:
            return None, exc.code

    competitor: str | None = None
    if scenario.scenario_id == "U01-away-and-back":
        clm.replace_contract(CONTRACT, {"risk_level": "medium", "risk_summary": "away"})
        clm.replace_contract(CONTRACT, {"risk_level": "high", "risk_summary": "new"})
        results = [request()]
    elif scenario.scenario_id == "U03-double-undo":
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: request(), range(2)))
    elif scenario.scenario_id == "U04-racing-write":
        version = clm.contract_version(CONTRACT)
        barrier = threading.Barrier(2)
        def meet_at_guard() -> None:
            barrier.wait(timeout=10)

        clm.before_guarded_write = meet_at_guard

        def other_write() -> str:
            try:
                clm.set_assessment(
                    "competitor", CONTRACT, "medium", "other", contract_version=version
                )
                return "completed"
            except ConnectorError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            other = pool.submit(other_write)
            undo = pool.submit(request)
            results, competitor = [undo.result()], other.result()
        if competitor != "completed":
            errors.append(competitor)
    else:
        results = [request()]
    for status, error in results:
        if status is not None:
            statuses.append(status)
        if error is not None:
            errors.append(error)
    accepted = clm.operations.records()
    restores = sum(r["operation_key"] == reversal_key(record.operation_key) for r in accepted)
    others = sum(r["operation_key"] == "competitor" for r in accepted)
    expected_rating = {"risk_level": "high", "summary": "before" if restores else "new"}
    if others:
        expected_rating = {"risk_level": "medium", "summary": "other"}
    undo_states = [r.state for r in stores.undo.for_run(REQUESTER.tenant_id, run.run_id)]
    actual: dict[str, Any] = {
        "scenario": scenario.scenario_id,
        "group": "undo",
        "original_status": original_status,
        "undo_statuses": sorted(statuses),
        "errors": sorted(errors),
        "new_runs": len(stores.runs.list_for_tenant(REQUESTER.tenant_id)) - 1,
        "accepted_restores": restores,
        "accepted_competitors": others,
        "final_rating_matches_winner": clm.current_assessment(CONTRACT)[0] == expected_rating,
        "undo_states": undo_states,
        "audit_valid": all(
            verify_chain(stores.audit.events(r.run_id)).ok
            for r in stores.runs.list_for_tenant(REQUESTER.tenant_id)
        ),
    }
    if scenario.scenario_id == "U04-racing-write":
        if (statuses == ["completed"] and competitor == "record_changed") or (
            statuses == ["failed"] and competitor == "completed"
        ):
            actual["undo_statuses"] = ["one_winner"]
        if restores in (0, 1):
            actual["accepted_restores"] = "zero_or_one"
        if others == 1 - restores:
            actual["accepted_competitors"] = "complement"
        if (restores == 1 and statuses == ["completed"] and undo_states == ["used"]) or (
            others == 1 and statuses == ["failed"] and undo_states == ["failed"]
        ):
            actual["undo_states"] = ["consistent_with_winner"]
    return actual


def expected_record(scenario: Scenario) -> dict[str, Any]:
    return {"scenario": scenario.scenario_id, "group": "undo", **scenario.expected}


def run_suite(
    results_dir: Path,
    *,
    stamp: str,
    git: tuple[str, bool],
    out: TextIO,
    scenarios: Sequence[Scenario] = SCENARIOS,
) -> bool:
    writer = ResultsWriter(results_dir / f"{stamp}-undo-integrity")
    writer.write_manifest(
        {
            "schema": "gawe.bench.undo.v1",
            "kind": KIND,
            "bench_run_id": f"{stamp}-undo-integrity",
            "git_commit": git[0],
            "git_dirty": git[1],
            "model": None,
            "scenarios": len(scenarios),
        }
    )
    mismatched = 0
    for scenario in scenarios:
        with tempfile.TemporaryDirectory(prefix="gawe-undo-bench-") as directory:
            actual = observe(scenario, Path(directory))
        diff = differences(actual, expected_record(scenario))
        writer.append(KIND, {**actual, "matches_expected": not diff, "differences": diff})
        mismatched += bool(diff)
        print(f"{'MISMATCH' if diff else 'ok':8} {scenario.scenario_id} {diff or ''}", file=out)
    writer.finish(
        complete=True, reason=None, spent_usd=Decimal(0), extra={"mismatched": mismatched}
    )
    print(
        f"undo: {len(scenarios) - mismatched}/{len(scenarios)} match their expected records",
        file=out,
    )
    print(f"wrote {writer.directory}", file=out)
    return mismatched == 0
