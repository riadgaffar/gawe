"""M26: unreadable prior state and undo records never authorize a mutation."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from gawe.api.undo import UndoRefused
from gawe.apps.contract_review.record_preparation import AssessmentPreparer
from gawe.connectors.base import Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.core.preparation import PreparationError
from gawe.core.records import StepRecord
from tests.adversarial.test_policy_unreadable import CASES, VARIABLES, decide
from tests.unit.test_undo_runs import rated
from tests.unit.test_write_readback import ALICE, Env

BAD_PRIORS = [
    {},
    [],
    False,
    "high",
    {"risk_level": "high"},
    {"summary": "prior"},
    {"risk_level": None, "summary": "prior"},
    {"risk_level": "unknown", "summary": "prior"},
    {"risk_level": "high", "summary": None},
    {"risk_level": "high", "summary": []},
    {"risk_level": "high", "summary": "x" * 501},
]


@pytest.mark.parametrize("version", ["3.0.0", "4.0.0"])
@pytest.mark.parametrize("current", BAD_PRIORS)
def test_unreadable_current_rating_fails_before_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current: Any,
    version: str,
) -> None:
    e = Env(tmp_path)
    e.stores.steps.add(
        StepRecord(
            run_id=e.run_id,
            step_id="write",
            tool="crm.write_assessment",
            tool_version=version,
            args_template=e.steps[0]["args"],
        )
    )
    if version == "3.0.0":
        monkeypatch.setattr(e.clm, "current_assessment", lambda _: (current, "v1"))
    else:
        # A malformed accessor result stays malformed; scalar "high" is not a record.
        raw = (
            {
                "deal_value": 45000,
                "risk_level": current.get("risk_level"),
                "risk_summary": current.get("summary"),
            }
            if isinstance(current, dict)
            else current
        )
        monkeypatch.setattr(e.clm, "current_contract", lambda _: (raw, "v1"))
    e.advance()
    step = e.stores.steps.get(e.run_id, "write")
    assert step.error == "record_version_unavailable"
    assert step.policy_decision is None
    assert e.clm.operations.records() == []
    assert e.stores.undo.for_run("t1", e.run_id) == []


def test_read_failure_and_absence_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path)

    def unreadable(_: str) -> Any:
        raise OSError("private connector error")

    monkeypatch.setattr(e.clm, "current_assessment", unreadable)
    with pytest.raises(PreparationError, match="record_version_unavailable"):
        AssessmentPreparer(e.clm)({"contract_id": "C-1001"}, reapproval=False)
    monkeypatch.undo()
    e.advance()
    assert e.stores.steps.get(e.run_id, "write").resolved_args["prior"] == {
        "risk_level": "none",
        "summary": "",
    }


@pytest.mark.parametrize("field", ["risk_level", "summary"])
@pytest.mark.parametrize("kind", ["missing", "null", "wrong_type"])
def test_nested_prior_cannot_pass_policy_schema(field: str, kind: str) -> None:
    args = deepcopy(CASES["contract_review"]["crm.write_assessment"][0])
    if kind == "missing":
        del args["prior"][field]
    else:
        args["prior"][field] = None if kind == "null" else []
    result = decide(
        "contract_review", "crm.write_assessment", args, deepcopy(VARIABLES["contract_review"])
    )
    assert result.outcome == "deny" and result.rule_ids == ("engine.invalid_args",)


@pytest.mark.parametrize(
    "prior",
    [
        {},
        {"risk_level": "high"},
        {"risk_level": "unknown", "summary": "prior"},
        {"risk_level": "high", "summary": "prior", "contract_id": "C-1002"},
    ],
)
def test_unreadable_armed_record_starts_no_run_or_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior: dict[str, str],
) -> None:
    e = rated(tmp_path)
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    monkeypatch.setattr(
        e.stores.undo, "for_run", lambda *_: [record.model_copy(update={"prior": prior})]
    )
    before = e.clm.operations.records()
    with pytest.raises(UndoRefused, match="undo_unavailable"):
        e.service.request_undo(ALICE, e.run_id, "write")
    assert len(e.stores.runs.list_for_tenant("t1")) == 1
    assert not [j for j in e.stores.maintenance.for_run("t1", e.run_id) if j.kind == "undo_request"]
    assert e.clm.operations.records() == before


@pytest.mark.parametrize("current", [None, {"risk_level": "high", "summary": ""}])
def test_absent_and_empty_summary_are_readable(
    monkeypatch: pytest.MonkeyPatch, current: Any
) -> None:
    clm = MockCLM({}, Ledger())
    monkeypatch.setattr(clm, "current_assessment", lambda _: (current, "v1"))
    prepared = AssessmentPreparer(clm)({"contract_id": "C-1001"}, reapproval=False)
    assert prepared.args["prior"] == (
        current if current is not None else {"risk_level": "none", "summary": ""}
    )
