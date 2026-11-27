"""M27 S3: a committed refusal survives a new resolver and Postgres store instances."""

from pathlib import Path
from typing import Any

import pytest

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, postgres_stores
from gawe.apps import contract_review
from gawe.connectors.base import Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.core.audit import verify_chain
from gawe.core.record_changes import RefusalDiagnostic
from gawe.llm.fake import FakeProvider
from tests.contract.conftest import ALICE, TENANT
from tests.flow import Clock, Crash
from tests.helpers import signer


def test_postgres_refusal_proof_recovers_without_reobserving_or_rewriting(
    pg_engine: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    clm = MockCLM.from_seed(
        contract_review.HERE / "samples/clm_seed.json", Ledger(tmp_path / "ledger.jsonl")
    )
    bundle = contract_review.bundle(tmp_path, clm=clm)
    stores = postgres_stores(pg_engine)
    who = Identity(TENANT, ALICE, frozenset({"case_manager"}))
    service = assemble(
        bundle,
        stores=stores,
        signer=signer(),
        background=InlineBackground(),
        clock=clock,
        provider=FakeProvider(
            [
                {
                    "steps": [
                        {
                            "id": "write",
                            "tool": "crm.write_assessment",
                            "args": {
                                "contract_id": "C-1001",
                                "risk_level": "high",
                                "summary": "review",
                            },
                        }
                    ]
                }
            ]
        ),
    )
    clm.before_guarded_write = lambda: clm.replace_contract("C-1001", {"deal_value": 150000})
    save = stores.maintenance.replace

    def crash_after_proof(job: Any, **kwargs: Any) -> Any:
        saved = save(job, **kwargs)
        if "refusal" in saved.evidence and saved.result is None:
            raise Crash()
        return saved

    monkeypatch.setattr(stores.maintenance, "replace", crash_after_proof)
    with pytest.raises(Crash):
        service.start_run(who, objective="rate", profile="contract_review")
    monkeypatch.undo()
    (run,) = stores.runs.list_for_tenant(TENANT)
    pending = next(
        j for j in stores.maintenance.for_run(TENANT, run.run_id) if j.kind == "write_readback"
    )
    assert pending.result is None and pending.evidence["refusal"]["code"] == "record_changed"
    clm.replace_contract("C-1001", {"deal_value": 250000})
    monkeypatch.setattr(
        clm, "current_contract", lambda _: pytest.fail("saved guard evidence must be used")
    )
    clock.advance(minutes=6)
    reloaded = postgres_stores(pg_engine)
    recovered = assemble(
        bundle,
        stores=reloaded,
        signer=signer(),
        background=InlineBackground(),
        clock=clock,
        provider=FakeProvider([]),
    )
    recovered.recover_interrupted()
    recovered._orch.advance(run.run_id)
    view = recovered.get_run(who, run.run_id)
    assert view.status == "failed"
    shown = view.steps[0].record_evidence
    assert shown is not None and shown.records[0].state == "changed"
    assert shown.records[0].snapshot.fields[0].value == 45000
    assert shown.records[0].observation.fields[0].value == 150000
    assert shown.records[0].observation.timing == "at_guard"
    final = reloaded.maintenance.get(TENANT, pending.job_id)
    assert final is not None and final.continued
    assert final.result == {"outcome": "not_dispatched", "code": "record_changed"}
    diagnostic = RefusalDiagnostic.model_validate(final.evidence["diagnostic"])
    assert diagnostic.observations[0].fields[0].value == 150000
    assert diagnostic.observations[0].timing == "at_guard"
    assert len(reloaded.executions.attempts(run.run_id, "write")) == 1
    assert clm.operations.records() == [] and verify_chain(reloaded.audit.events(run.run_id)).ok
