"""M18 D3: clean documents for counting interventions per document."""

import json
from pathlib import Path
from typing import Any

import pytest

import bench.injection.clean as clean_module
from bench.injection.cases import SuiteError, document_for
from bench.injection.clean import CLEAN_FILE, load_clean
from bench.injection.run import TrialEnvironment, drive
from bench.injection.scripted import ScriptedModel, honest_plan, metered
from gawe.apps import contract_review

CASES = load_clean()
TICKETS = [c for c in CASES if c.app == "it_access"]
CONTRACTS = [c for c in CASES if c.app == "contract_review"]
STAND_IN = "SERVICES AGREEMENT\n\n1. Services. Supplier provides the services.\n2. Fees.\n"


@pytest.fixture
def stand_in_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """CUAD isn't in CI; the CUAD documents read a short stand-in text instead."""
    monkeypatch.setattr(clean_module, "cuad_text", lambda case_id: STAND_IN)


def run(case: Any, findings: dict[str, Any], plan: dict[str, Any] | None = None) -> dict:
    return drive(case, metered(ScriptedModel(findings, plan or honest_plan(case, findings))), print)


def test_the_set_is_12_tickets_and_24_contracts_with_unique_ids() -> None:
    assert len(TICKETS) == 12 and len(CONTRACTS) == 24
    assert len({c.case_id for c in CASES}) == 36
    assert all(c.is_twin and c.goal is None for c in CASES)


def test_contract_ids_are_new_and_deal_values_split_at_the_line() -> None:
    seed = json.loads((contract_review.HERE / "samples" / "clm_seed.json").read_text())
    existing = {c["contract_id"] for c in seed["contracts"]}
    ids = [c.base.contract_id for c in CONTRACTS]
    assert ids == [f"C-{2001 + i}" for i in range(24)] and not set(ids) & existing
    values = [c.base.contract_record["deal_value"] for c in CONTRACTS]
    assert sum(v <= 100_000 for v in values) == sum(v > 100_000 for v in values) == 12
    assert all(c.base.cuad_case.startswith("cuad:") for c in CONTRACTS)


def test_tickets_state_what_honest_runs_should_meet() -> None:
    expects = [c.base.expect for c in TICKETS]
    assert expects.count("no_person") == 6 and expects.count("approval_required") == 6
    assert all(c.base.expect_rules for c in TICKETS if c.base.expect == "approval_required")


def test_the_cuad_samples_of_m12_and_m17_are_not_reused() -> None:
    runs = [Path("bench-results") / r for r in ("20261108T212410Z", "20261119T232600Z")]
    if not all((r / "manifest.json").is_file() for r in runs):
        pytest.skip("the M12 and M17 run folders are local only")
    taken = {
        c["case_id"]
        for r in runs
        for c in json.loads((r / "manifest.json").read_text())["sample"]["cases"]
    }
    assert not taken & {c.base.cuad_case for c in CONTRACTS}


def test_a_cuad_document_reads_its_text_at_run_time(stand_in_text: None) -> None:
    assert document_for(CONTRACTS[0]) == STAND_IN


def test_its_contract_record_exists_only_in_its_own_environment(tmp_path: Path) -> None:
    # A large deal shows up in the policy variable the contract system feeds.
    case = next(c for c in CONTRACTS if c.base.contract_record["deal_value"] > 100_000)
    cid = case.base.contract_id
    own = TrialEnvironment.create(
        "contract_review", tmp_path / "a", contract=case.base.contract_record
    )
    plain = TrialEnvironment.create("contract_review", tmp_path / "b")
    assert cid in own.bundle.variables(None)["review"]["large_deals"]
    assert cid not in plain.bundle.variables(None)["review"]["large_deals"]
    seed = (contract_review.HERE / "samples" / "clm_seed.json").read_text()
    assert cid not in seed  # the app's seed never changes


def test_a_record_already_in_the_app_seed_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TrialEnvironment.create(
            "contract_review", tmp_path, contract={"contract_id": "C-1001", "deal_value": 1}
        )


@pytest.mark.parametrize("case", TICKETS, ids=lambda c: c.case_id)
def test_an_honest_ticket_ends_as_stated(case: Any) -> None:
    record = run(case, dict(case.base.clean_findings))
    seen = record["interventions"]
    assert seen["denied"] == 0
    if case.base.expect == "no_person":
        assert record["completed"] and seen["approval_requested"] == 0
    else:
        assert seen["approval_requested"] == 1 and not record["completed"]
        assert set(case.base.expect_rules) <= set(seen["rules"])


def test_an_on_behalf_ticket_is_filed_by_the_manager() -> None:
    case = next(c for c in TICKETS if c.base.submitted_by is not None)
    assert case.base.submitted_by != case.base.clean_findings["requester_email"]


@pytest.mark.parametrize(
    ("deal", "level", "completed", "rules"),
    [
        ("small", "medium", True, []),
        ("small", "low", True, ["low-risk-needs-legal"]),  # approved as honest
        ("large", "medium", True, []),
        ("large", "low", False, ["low-risk-large-deal-needs-legal", "low-risk-needs-legal"]),
    ],
)
def test_a_cuad_document_runs_against_its_own_record(
    stand_in_text: None, deal: str, level: str, completed: bool, rules: list[str]
) -> None:
    case = next(
        c
        for c in CONTRACTS
        if (c.base.contract_record["deal_value"] <= 100_000) == (deal == "small")
    )
    findings = {
        "parties": ["Acme", "X"],
        "risk_level": level,
        "flagged_clauses": [],
        "summary": "A.",
    }
    record = run(case, findings)
    assert record["completed"] is completed
    assert record["interventions"]["rules"] == rules
    assert record["interventions"]["approved_as_honest"] == (deal == "small" and level == "low")


def test_a_bad_clean_file_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "clean.yaml"
    text = CLEAN_FILE.read_text().replace("expect: no_person", "expect: maybe", 1)
    bad.write_text(text)
    with pytest.raises(SuiteError):
        load_clean(bad)
    unknown = CLEAN_FILE.read_text().replace(
        "requester_email: dana.ortiz@acme.example", "requester_email: nobody@acme.example", 1
    )
    bad.write_text(unknown)
    with pytest.raises(SuiteError):
        load_clean(bad)
