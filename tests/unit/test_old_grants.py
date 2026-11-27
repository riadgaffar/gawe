"""M21: grants made before M16b have no end date; GAWE ends them on one it sets.

S1: the mock IAM can read and revoke them, wherever they live.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from gawe.api.wiring import load_app_rules, memory_stores
from gawe.apps import contract_review, it_access
from gawe.connectors.base import Ledger
from gawe.connectors.mock_iam import MockIAM
from gawe.core.executor import idempotency_key
from gawe.core.old_grants import (
    DEFAULT_NOTICE_DAYS,
    end_date,
    find_old_grants,
    notice_period,
)
from gawe.core.readback import operation_identity
from gawe.core.records import StepRecord
from gawe.core.runs import RunRecord
from gawe.core.statemachine import StepStatus

DANA = "dana.ortiz@acme.example"
NOW = datetime.fromisoformat("2026-11-22T12:00:00+00:00")
LATER = NOW + timedelta(days=3650)
SEED = it_access.HERE / "samples/iam_seed.json"
OLD_V1 = ("iam.grant_access", "1.0.0")


def iam_at(path: Path) -> MockIAM:
    return MockIAM.from_seed(SEED, Ledger(path / "ledger.jsonl"))


def ledger_only_grant(path: Path, key: str) -> None:
    """How the mock IAM recorded a grant from M9 to M15: the ledger, nothing else."""
    Ledger(path / "ledger.jsonl").record_once(
        "iam.grant", key, lambda: {"email": DANA, "system": "wiki", "level": "read"}
    )


def v1_store_grant(iam: MockIAM, key: str) -> None:
    """How a 1.0.0 grant was recorded from M16a to M16b: the separate 1.0.0 store."""
    iam.legacy_operations.accept(key, {"email": DANA, "system": "wiki", "level": "read"})


@pytest.mark.parametrize("kind", ["ledger", "v1_store"])
def test_an_old_grant_is_active_until_revoked_then_reads_back_revoked(
    tmp_path: Path, kind: str
) -> None:
    if kind == "ledger":
        ledger_only_grant(tmp_path, "old")
    iam = iam_at(tmp_path)
    if kind == "v1_store":
        v1_store_grant(iam, "old")
    assert iam.grant_state("old", LATER) == "active"  # no end date of its own
    assert iam.overdue(LATER) == 0  # the system can't know it's overdue; GAWE's jobs do (S3)
    iam.revoke("rk", "old", NOW)
    assert iam.grant_state("old", NOW) == "revoked"


def test_revoking_an_old_grant_twice_records_once(tmp_path: Path) -> None:
    iam = iam_at(tmp_path)
    v1_store_grant(iam, "old")
    iam.revoke("rk", "old", NOW)
    iam.revoke("rk", "old", NOW + timedelta(hours=1))
    assert [r["grant_key"] for r in iam.revocations()] == ["old"]
    assert iam.revocations()[0]["revoked_at"] == NOW.isoformat()


def test_a_key_no_store_knows_reads_as_absent(tmp_path: Path) -> None:
    iam = iam_at(tmp_path)
    assert iam.grant_state("never", NOW) == "absent"
    iam.revoke("rk", "never", NOW)  # revoking nothing doesn't invent a grant
    assert iam.grant_state("never", NOW) == "absent"


def test_a_new_process_sees_old_grants_and_their_revocation(tmp_path: Path) -> None:
    ledger_only_grant(tmp_path, "from-ledger")
    first = iam_at(tmp_path)
    v1_store_grant(first, "from-v1")
    first.revoke("rk", "from-v1", NOW)
    second = iam_at(tmp_path)  # a sweep in another process
    assert second.grant_state("from-ledger", NOW) == "active"
    assert second.grant_state("from-v1", NOW) == "revoked"


def test_current_grants_still_follow_their_own_end_date(tmp_path: Path) -> None:
    iam = iam_at(tmp_path)
    iam.operations.accept("new", {"expires_at": (NOW + timedelta(days=7)).isoformat()})
    assert iam.grant_state("new", NOW) == "active"
    assert iam.grant_state("new", NOW + timedelta(days=7)) == "expired"
    assert iam.overdue(NOW + timedelta(days=7)) == 1


# --- startup -------------------------------------------------------------------------------------


def test_the_it_app_revokes_old_grants_but_expires_only_current_ones_at_dispatch(
    tmp_path: Path,
) -> None:
    from tests.helpers import signer

    from gawe.api.background import InlineBackground
    from gawe.api.wiring import assemble, memory_stores
    from gawe.llm.fake import FakeProvider

    b = it_access.bundle(tmp_path)
    assert OLD_V1 in b.revokers and load_app_rules(b)
    service = assemble(
        b,
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider([]),
        background=InlineBackground(),
    )
    verification = service._orch._executor.verification
    assert verification is not None
    assert verification.expiring == frozenset(
        {("iam.grant_access", "2.0.0"), ("iam.grant_access", "3.0.0")}
    )


def test_only_a_retired_version_of_an_expiring_tool_may_have_a_revoker(tmp_path: Path) -> None:
    b = it_access.bundle(tmp_path)
    iam = b.revokers[OLD_V1]
    without_current = {OLD_V1: iam}
    with pytest.raises(ValueError, match="missing"):
        load_app_rules(replace(b, revokers=without_current))
    with pytest.raises(ValueError, match="unexpected"):
        load_app_rules(replace(b, revokers={**b.revokers, ("iam.lookup_user", "1.0.0"): iam}))
    # A retired write whose tool has no version with an end date may not have one.
    cr = contract_review.bundle(tmp_path / "cr")
    with pytest.raises(ValueError, match="unexpected"):
        load_app_rules(replace(cr, revokers={("crm.write_assessment", "1.0.0"): iam}))


# --- S2: finding old grants and their end dates -------------------------------------------------

LIFETIME = timedelta(days=7)
NOTICE = timedelta(days=DEFAULT_NOTICE_DAYS)
H = "a" * 64


def test_the_end_date_is_the_later_of_the_natural_end_and_the_notice() -> None:
    old = NOW - timedelta(days=30)
    assert end_date(old, lifetime=LIFETIME, backfill_at=NOW, notice=NOTICE) == (
        NOW + NOTICE,
        "notice",
    )
    recent = NOW - timedelta(days=1)
    long = timedelta(days=30)
    assert end_date(recent, lifetime=long, backfill_at=NOW, notice=NOTICE) == (
        recent + long,
        "lifetime",
    )
    # A tie is the natural end: the grant ends when a new one would have.
    tie = NOW
    assert end_date(tie, lifetime=NOTICE, backfill_at=NOW, notice=NOTICE) == (
        NOW + NOTICE,
        "lifetime",
    )


@pytest.mark.parametrize("days", [0, -1, 31, 365])
def test_the_notice_period_is_one_to_thirty_days(days: int) -> None:
    with pytest.raises(ValueError, match="1 to 30 days"):
        notice_period(days)
    assert notice_period(1) == timedelta(days=1) and notice_period(30) == timedelta(days=30)


class History:
    """GAWE's own records of grants sent over the years, in memory stores."""

    def __init__(self) -> None:
        self.stores = memory_stores()

    def run(self, run_id: str, tenant: str = "t1") -> None:
        self.stores.runs.add(
            RunRecord(
                run_id=run_id,
                tenant_id=tenant,
                acting_user_id="alice",
                objective="o",
                profile="it_access",
                created_at=NOW - timedelta(days=60),
            )
        )

    def step(
        self,
        run_id: str,
        step_id: str,
        status: StepStatus,
        *,
        version: str = "1.0.0",
        attempts: list[tuple[str, str | None, datetime]] = (),  # type: ignore[assignment]
    ) -> None:
        """`attempts`: (key, "succeeded" | "failed" | None for never finished, start time)."""
        hashed = status not in {StepStatus.PENDING, StepStatus.READY, StepStatus.FAILED}
        self.stores.steps.add(
            StepRecord(
                run_id=run_id,
                step_id=step_id,
                tool="iam.grant_access",
                tool_version=version,
                status=status,
                action_hash=H if hashed else None,
            )
        )
        for number, (key, outcome, started) in enumerate(attempts, start=1):
            ex = self.stores.executions
            ex.start(run_id, step_id, attempt=number, idempotency_key=key, now=started)
            if outcome is not None:
                ex.finish(
                    run_id,
                    step_id,
                    attempt=number,
                    status=outcome,  # type: ignore[arg-type]
                    now=started + timedelta(seconds=5),
                )

    def find(self) -> list:  # type: ignore[type-arg]
        return find_old_grants(
            [OLD_V1],
            steps=self.stores.steps,
            executions=self.stores.executions,
            runs=self.stores.runs,
        )


def test_old_grants_are_found_from_gawes_own_records_in_both_eras() -> None:
    h = History()
    pre_m16a = NOW - timedelta(days=20)
    m16a = NOW - timedelta(days=9)
    h.run("r-old")
    h.step(
        "r-old",
        "grant",
        StepStatus.SUCCEEDED,
        attempts=[(idempotency_key(H), "succeeded", pre_m16a)],
    )
    h.run("r-m16a")
    era = operation_identity(H, 1)
    h.step(
        "r-m16a",
        "grant",
        StepStatus.SUCCEEDED,
        attempts=[(era, "failed", m16a), (era, "succeeded", m16a + timedelta(minutes=1))],
    )
    found = {g.run_id: g for g in h.find()}
    assert set(found) == {"r-old", "r-m16a"}
    assert found["r-old"].grant_key == idempotency_key(H)
    assert found["r-old"].made_at == pre_m16a + timedelta(seconds=5)
    assert found["r-m16a"].grant_key == era  # the attempt that succeeded
    assert found["r-m16a"].made_at == m16a + timedelta(minutes=1, seconds=5)
    assert {g.tenant_id for g in found.values()} == {"t1"}


def test_a_grant_whose_outcome_is_unknown_counts_and_uses_when_it_was_sent() -> None:
    h = History()
    sent = NOW - timedelta(days=12)
    h.run("r1")
    h.step("r1", "grant", StepStatus.VERIFICATION_UNKNOWN, attempts=[("k1", None, sent)])
    h.run("r2")
    h.step("r2", "grant", StepStatus.VERIFICATION_BLOCKED, attempts=[("k2", "failed", sent)])
    found = {g.run_id: g for g in h.find()}
    assert found["r1"].made_at == sent and found["r1"].step_status == "verification_unknown"
    assert found["r2"].grant_key == "k2"


def test_nothing_gawe_did_not_send_is_counted() -> None:
    h = History()
    h.run("r1")
    h.step("r1", "failed", StepStatus.FAILED, attempts=[("k-failed", "failed", NOW)])
    h.step("r1", "never-sent", StepStatus.READY)
    h.step("r1", "denied", StepStatus.DENIED)
    h.step("r1", "succeeded-without-attempt", StepStatus.SUCCEEDED)  # nothing to name it by
    h.step(
        "r1",
        "current",
        StepStatus.SUCCEEDED,
        version="2.0.0",
        attempts=[("k-new", "succeeded", NOW)],
    )
    assert h.find() == []


def test_tenants_are_kept_apart() -> None:
    h = History()
    for run, tenant in (("r1", "t1"), ("r2", "t2")):
        h.run(run, tenant)
        h.step(run, "grant", StepStatus.SUCCEEDED, attempts=[(f"k-{run}", "succeeded", NOW)])
    assert {(g.run_id, g.tenant_id) for g in h.find()} == {("r1", "t1"), ("r2", "t2")}


def test_the_it_app_declares_the_lifetime_it_grants_today(tmp_path: Path) -> None:
    assert it_access.bundle(tmp_path).grant_lifetime == timedelta(hours=168)
    assert it_access.bundle(tmp_path, grant_lifetime_hours=24).grant_lifetime == timedelta(hours=24)
    assert contract_review.bundle(tmp_path / "cr").grant_lifetime is None
