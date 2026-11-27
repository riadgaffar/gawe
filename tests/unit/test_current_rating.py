"""M26 S1: the contract system keeps a current rating, its own writes move the revision, and it
can restore an earlier rating only at the revision recorded after the write it reverses."""

import threading
import time
from pathlib import Path

import pytest

from gawe.apps import contract_review
from gawe.connectors.base import ConnectorError, Ledger
from gawe.connectors.credentials import Credential, CredentialScopeError, ScopedClient
from gawe.connectors.mock_clm import MockCLM

SEED = contract_review.HERE / "samples/clm_seed.json"
C = "C-1001"


@pytest.fixture
def clm(tmp_path: Path) -> MockCLM:
    return MockCLM.from_seed(SEED, Ledger(tmp_path / "ledger.jsonl"))


def rate(clm: MockCLM, key: str, level: str, version: str) -> dict[str, object]:
    return clm.set_assessment(key, C, level, f"{level} summary", contract_version=version)


def operation(clm: MockCLM, key: str) -> dict[str, object] | None:
    return next((r for r in clm.operations.records() if r["operation_key"] == key), None)


def test_no_rating_in_the_seed_and_tools_never_see_one(clm: MockCLM) -> None:
    prior, version = clm.current_assessment(C)
    assert prior is None
    rate(clm, "k1", "high", version)
    assert clm.current_assessment(C)[0] == {"risk_level": "high", "summary": "high summary"}
    contract = clm.get_contract(C)
    assert "risk_level" not in contract and "risk_summary" not in contract


def test_the_systems_own_write_moves_the_revision(clm: MockCLM) -> None:
    _, before = clm.current_assessment(C)
    rate(clm, "k1", "high", before)
    _, after = clm.current_assessment(C)
    assert after != before
    assert operation(clm, "k1") == {
        "operation_key": "k1",
        "assessment_id": "as-k1",
        "contract_id": C,
        "risk_level": "high",
        "summary": "high summary",
        "record": {"contract_version": before},
        "result_version": after,
    }
    assert clm.replace_contract(C, {"status": "renewed"}) != after  # anyone else's change too


def test_a_retry_under_the_same_key_changes_nothing(clm: MockCLM) -> None:
    _, before = clm.current_assessment(C)
    rate(clm, "k1", "high", before)
    _, after = clm.current_assessment(C)
    rate(clm, "k1", "high", before)  # the retry after a lost reply
    assert clm.current_assessment(C)[1] == after
    assert sum(r["operation_key"] == "k1" for r in clm.operations.records()) == 1


def test_the_lost_update_second_write_on_one_revision_is_refused(clm: MockCLM) -> None:
    _, version = clm.current_assessment(C)
    rate(clm, "first", "high", version)
    with pytest.raises(ConnectorError) as refused:
        rate(clm, "second", "low", version)  # approved against the original rating
    assert refused.value.code == "record_changed"
    assert operation(clm, "second") is None
    assert clm.current_assessment(C)[0] == {"risk_level": "high", "summary": "high summary"}


def test_two_racing_writes_on_one_revision_one_lands(clm: MockCLM) -> None:
    _, version = clm.current_assessment(C)
    clm.before_guarded_write = lambda: time.sleep(0.05)  # both pass the separate read first
    outcomes: list[str] = []

    def go(key: str, level: str) -> None:
        try:
            rate(clm, key, level, version)
            outcomes.append("applied")
        except ConnectorError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=go, args=a) for a in (("a", "high"), ("b", "low"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["applied", "record_changed"]


def test_restore_puts_back_the_prior_rating_with_a_new_revision(clm: MockCLM) -> None:
    _, v0 = clm.current_assessment(C)
    rate(clm, "k1", "high", v0)
    prior, v1 = clm.current_assessment(C)
    rate(clm, "k2", "low", v1)
    _, v2 = clm.current_assessment(C)
    reply = clm.restore_assessment("u2", C, prior, contract_version=v2)
    assert reply == {"restore_id": "rs-u2", "contract_id": C, "restored_level": "high"}
    restored, v3 = clm.current_assessment(C)
    assert restored == prior
    assert v3 not in {v0, v1, v2}
    assert operation(clm, "u2") == {
        "operation_key": "u2",
        "restore_id": "rs-u2",
        "contract_id": C,
        "restored": prior,
        "record": {"contract_version": v2},
        "result_version": v3,
    }


def test_restore_to_no_rating_removes_it(clm: MockCLM) -> None:
    _, v0 = clm.current_assessment(C)
    rate(clm, "k1", "low", v0)
    _, v1 = clm.current_assessment(C)
    reply = clm.restore_assessment("u1", C, None, contract_version=v1)
    assert reply["restored_level"] is None
    assert clm.current_assessment(C)[0] is None


def test_restore_after_a_change_away_and_back_is_refused(clm: MockCLM) -> None:
    _, v0 = clm.current_assessment(C)
    rate(clm, "k1", "low", v0)
    _, v1 = clm.current_assessment(C)
    # Someone else changes the rating and then changes it back: the values match again.
    clm.replace_contract(C, {"risk_level": "medium", "risk_summary": "medium"})
    clm.replace_contract(C, {"risk_level": "low", "risk_summary": "low summary"})
    assert clm.current_assessment(C)[0] == {"risk_level": "low", "summary": "low summary"}
    with pytest.raises(ConnectorError) as refused:
        clm.restore_assessment("u1", C, None, contract_version=v1)
    assert refused.value.code == "record_changed"
    assert operation(clm, "u1") is None
    assert clm.current_assessment(C)[0] == {"risk_level": "low", "summary": "low summary"}


def test_restore_is_once_per_key(clm: MockCLM) -> None:
    _, v0 = clm.current_assessment(C)
    rate(clm, "k1", "low", v0)
    _, v1 = clm.current_assessment(C)
    first = clm.restore_assessment("u1", C, None, contract_version=v1)
    _, v2 = clm.current_assessment(C)
    again = clm.restore_assessment("u1", C, None, contract_version=v1)  # its revision is stale now
    assert again == first
    assert clm.current_assessment(C)[1] == v2
    assert sum(r["operation_key"] == "u1" for r in clm.operations.records()) == 1


def test_restoring_needs_its_own_scope(clm: MockCLM) -> None:
    _, v0 = clm.current_assessment(C)
    rate(clm, "k1", "low", v0)
    _, v1 = clm.current_assessment(C)
    scopes = frozenset({"assessment:write"})
    writer = ScopedClient(clm, Credential("gawe-tool:crm.write_assessment", scopes))
    with pytest.raises(CredentialScopeError):
        writer.restore_assessment("u1", C, None, contract_version=v1)
    assert operation(clm, "u1") is None


def test_the_2_0_0_write_still_only_appends(clm: MockCLM) -> None:
    _, before = clm.current_assessment(C)
    clm.write_assessment("old", C, "high", "s", contract_version=before)
    assert clm.current_assessment(C) == (None, before)
