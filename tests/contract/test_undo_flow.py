"""The reversal request/run/read-back path on durable Postgres stores."""

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
from gawe.llm.fake import FakeProvider
from tests.contract.conftest import ALICE, TENANT
from tests.helpers import signer


def test_postgres_undo_run_and_record(pg_engine: Any, tmp_path: Path) -> None:
    clm = MockCLM.from_seed(
        contract_review.HERE / "samples/clm_seed.json", Ledger(tmp_path / "ledger.jsonl")
    )
    clm.set_assessment(
        "seed", "C-1001", "high", "before", contract_version=clm.contract_version("C-1001")
    )
    stores = postgres_stores(pg_engine)
    who = Identity(TENANT, ALICE, frozenset({"case_manager"}))
    service = assemble(
        contract_review.bundle(tmp_path, clm=clm),
        stores=stores,
        signer=signer(),
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
                                "summary": "after",
                            },
                        }
                    ]
                }
            ]
        ),
        background=InlineBackground(),
    )
    original = service.start_run(who, objective="rate", profile="contract_review")
    undone = service.request_undo(who, original.run_id, "write")
    assert undone.status == "completed"
    assert stores.undo.for_run(TENANT, original.run_id)[0].state == "used"
    assert clm.current_assessment("C-1001")[0] == {"risk_level": "high", "summary": "before"}
    assert verify_chain(stores.audit.events(original.run_id)).ok
    assert verify_chain(stores.audit.events(undone.run_id)).ok


def test_postgres_undo_request_recovers_original_snapshots(
    pg_engine: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gawe.core.undo import reversal_run_id
    from tests.flow import Crash

    clm = MockCLM.from_seed(
        contract_review.HERE / "samples/clm_seed.json", Ledger(tmp_path / "ledger.jsonl")
    )
    clm.set_assessment(
        "seed", "C-1001", "high", "before", contract_version=clm.contract_version("C-1001")
    )
    bundle = contract_review.bundle(tmp_path, clm=clm)
    stores = postgres_stores(pg_engine)
    who = Identity(TENANT, ALICE, frozenset({"case_manager"}))
    service = assemble(
        bundle,
        stores=stores,
        signer=signer(),
        background=InlineBackground(),
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
                                "summary": "after",
                            },
                        }
                    ]
                }
            ]
        ),
    )
    original = service.start_run(who, objective="rate", profile="contract_review")
    record = stores.undo.for_run(TENANT, original.run_id)[0]
    assert record.snapshots and record.state == "armed"

    def crash(_: Any) -> Any:
        raise Crash()

    monkeypatch.setattr(service._undo, "ensure", crash)
    with pytest.raises(Crash):
        service.request_undo(who, original.run_id, "write")
    monkeypatch.undo()
    reloaded = postgres_stores(pg_engine)
    recovered = assemble(
        bundle,
        stores=reloaded,
        signer=signer(),
        background=InlineBackground(),
        provider=FakeProvider([]),
    )
    recovered.maintenance_runner.tick()
    step = reloaded.steps.get(reversal_run_id(record), "restore")
    assert step.status.value == "succeeded" and step.tool_version == "2.0.0"
    assert step.resolved_args["snapshots"][0]["source_action_hash"] == record.action_hash
    assert step.resolved_args["snapshots"][0]["revision"] == record.bound_version
    assert reloaded.undo.get(TENANT, record.undo_id).snapshots == record.snapshots
    assert len(clm.operations.records()) == 3
    assert verify_chain(reloaded.audit.events(original.run_id)).ok
    assert verify_chain(reloaded.audit.events(reversal_run_id(record))).ok

    shown = recovered.get_run(who, reversal_run_id(record)).steps[0].record_evidence
    assert shown is not None and shown.records[0].snapshot.basis == "confirmed_original"
    assert shown.records[0].snapshot.source_action_hash == record.action_hash
    assert shown.records[0].guard_revision == record.result_version
