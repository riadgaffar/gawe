"""M23 S3: the step trace over the Postgres stores links an approved write with no gaps.

The trace matches audit events by rebuilding their payloads from stored records (an approval's
expiry, a decision's checks), so a value that changes form on its way through the database would
show as a false gap here.
"""

from pathlib import Path
from typing import Any

from gawe.api.auth import Identity
from gawe.core.statemachine import RunStatus
from tests.contract.conftest import ALICE, BOB, TENANT
from tests.contract.test_readback_postgres import PLAN, World

LOW = {
    "steps": [
        {**PLAN["steps"][0], "args": {**PLAN["steps"][0]["args"], "risk_level": "low"}},
    ]
}


def test_an_approved_write_traces_with_no_gaps(
    pg_engine: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr("tests.contract.test_readback_postgres.PLAN", LOW)
    w = World(pg_engine, tmp_path)  # a "low" assessment waits for legal (M17)
    assert w.service._orch.advance(w.run_id).status is RunStatus.PAUSED_FOR_APPROVAL
    reviewer = Identity(TENANT, BOB, frozenset({"legal_reviewer"}))
    (item,) = w.service.inbox(reviewer)
    w.service.decide(reviewer, item.approval_id, decision="approved", action_hash=item.action_hash)
    assert w.service._runs.get(w.run_id).status is RunStatus.COMPLETED

    caller = Identity(TENANT, ALICE, frozenset({"case_manager"}))
    t = w.service.step_trace(caller, w.run_id, "write")
    assert t.gaps == ()
    assert t.decision is not None and t.decision.audited and t.decision.signature_valid
    (approval,) = t.approvals
    assert approval.current and approval.decision == "approved" and approval.audited
    (attempt,) = t.attempts
    (readback,) = t.readback
    assert readback.current and readback.operation_key == attempt.operation_key
