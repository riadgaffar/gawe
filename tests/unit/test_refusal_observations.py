"""M27 S3: durable refusal evidence never becomes retry authority or invented history."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.flow import Crash
from tests.unit.test_apps_event_replay import OPERATOR, Desk, replay_ids
from tests.unit.test_maintenance_runner import restart
from tests.unit.test_record_changes import observation
from tests.unit.test_undo_runs import rated
from tests.unit.test_write_readback import ALICE, TOOLS, Env

from gawe.connectors.base import ConnectorError
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.maintenance import JobConflictError
from gawe.core.record_changes import RefusalDiagnostic


def diagnostic(e: Env) -> RefusalDiagnostic:
    return RefusalDiagnostic.model_validate(e.job.evidence["diagnostic"])


def fallback(e: Env, change: Any = None, observations: tuple = ()) -> None:
    def refused(args: Any, call: Any) -> Any:
        e.calls.append(call)
        if change:
            change()
        raise ConnectorError("record_changed", observations=observations)

    e.orch._executor._tools[TOOLS[e.system]] = refused


@pytest.mark.parametrize("system", ["crm", "esign", "iam"])
def test_atomic_guard_observations_are_selected_and_bound(tmp_path: Path, system: str) -> None:
    e = Env(tmp_path, system)
    if system == "iam":
        e.iam.before_guarded_write = lambda: e.iam.replace_user(
            "dana.ortiz@acme.example", {"department": "secret"}
        )
    else:
        e.clm.before_guarded_write = lambda: e.clm.replace_contract(
            "C-1001", {"deal_value": 150000, "note": "secret"}
        )
    e.execute()
    saved = diagnostic(e)
    step = e.stores.steps.get(e.run_id, "write")
    assert saved.action_hash == step.action_hash and saved.coverage == "complete"
    assert saved.code == step.error == "record_changed"
    (seen,) = saved.observations
    assert seen.timing == "at_guard" and seen.status == "observed"
    assert [f.path for f in seen.fields] == ([] if system == "iam" else ["deal_value"])
    assert "secret" not in saved.model_dump_json()
    assert e.target.records() == [] and len(e.calls) == 1
    assert e.job.result == {"outcome": "not_dispatched", "code": "record_changed"}
    event = next(v for v in e.stores.audit.events(e.run_id) if v.type == "verification.readback")
    assert event.payload_hash == sha256_hex(
        canonical_json(
            {
                "step_id": "write",
                "operation_key": e.job.operation_key,
                "evidence_hash": sha256_hex(canonical_json(e.job.evidence)),
                "status": "failed",
                "code": "record_changed",
            }
        )
    )


def test_guard_evidence_survives_a_later_edit_without_a_diagnostic_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path)
    impl = e.orch._executor._tools[TOOLS["crm"]]
    e.clm.before_guarded_write = lambda: e.clm.replace_contract("C-1001", {"deal_value": 150000})

    def raced(args: Any, call: Any) -> Any:
        try:
            return impl(args, call)
        except ConnectorError:
            e.clm.replace_contract("C-1001", {"deal_value": 250000})
            monkeypatch.setattr(
                e.clm, "current_contract", lambda _: pytest.fail("must use guard evidence")
            )
            raise

    e.orch._executor._tools[TOOLS["crm"]] = raced
    e.execute()
    assert diagnostic(e).observations[0].fields[0].value == 150000


@pytest.mark.parametrize(
    "mode,status,reason",
    [
        ("read", "observed", None),
        ("missing", "missing", None),
        ("unreadable", "unavailable", "unreadable"),
        ("invalid", "unavailable", "invalid"),
    ],
)
def test_later_reads_are_labeled_and_failures_never_erase_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, status: str, reason: str | None
) -> None:
    e = Env(tmp_path)

    def change() -> None:
        if mode == "read":
            e.clm.replace_contract("C-1001", {"deal_value": 250000})
        elif mode == "invalid":
            monkeypatch.setattr(
                e.clm, "current_contract", lambda _: ({"deal_value": ["secret"]}, "v2")
            )
        else:

            def unavailable(_: str) -> Any:
                if mode == "missing":
                    raise ConnectorError("contract_not_found")
                raise OSError("private error")

            monkeypatch.setattr(e.clm, "current_contract", unavailable)

    fallback(e, change)
    e.execute()
    seen = diagnostic(e).observations[0]
    assert (seen.timing, seen.status, seen.reason) == ("after_refusal", status, reason)
    if mode == "read":
        assert seen.fields[0].value == 250000
    assert "secret" not in repr(e.job.evidence) and "private error" not in repr(e.job.evidence)
    assert len(e.calls) == 1 and e.clm.operations.records() == []


@pytest.mark.parametrize(
    "stage,guarded", [("proof", False), ("proof", True), ("final", False), ("final", True)]
)
def test_crash_after_saved_proof_or_diagnostic_recovers_without_another_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, guarded: bool
) -> None:
    e = Env(tmp_path)
    if guarded:
        e.clm.before_guarded_write = lambda: e.clm.replace_contract(
            "C-1001", {"deal_value": 150000}
        )
    else:
        fallback(e, lambda: e.clm.replace_contract("C-1001", {"deal_value": 150000}))
    save = e.stores.maintenance.replace

    def crash(job: Any, **kw: Any) -> Any:
        result = save(job, **kw)
        if "refusal" in job.evidence and (
            job.result is None if stage == "proof" else "diagnostic" in job.evidence
        ):
            raise Crash()
        return result

    monkeypatch.setattr(e.stores.maintenance, "replace", crash)
    with pytest.raises(Crash):
        e.advance()
    before = e.job
    assert before.evidence["refusal"]["code"] == "record_changed"
    monkeypatch.undo()
    e.clm.replace_contract("C-1001", {"deal_value": 250000})
    e.clock.advance(minutes=6)  # Even past the verification deadline, this is a proven refusal.
    service = restart(e)
    service.recover_interrupted()
    service._orch.advance(e.run_id)
    after = diagnostic(e)
    assert service.get_run(ALICE, e.run_id).status == "failed"
    assert e.job.result["code"] == "record_changed" and e.job.continued
    expected = 150000 if guarded or stage == "final" else 250000
    assert after.observations[0].fields[0].value == expected
    assert after.observations[0].timing == ("at_guard" if guarded else "after_refusal")
    if stage == "final":
        assert e.job.evidence == before.evidence
    assert len(e.calls) == 1 and e.clm.operations.records() == []


def test_crash_during_diagnostic_read_recovers_as_a_later_observation(tmp_path: Path) -> None:
    e = Env(tmp_path)
    resolver = e.orch._executor.verification
    assert resolver is not None
    key = (TOOLS["crm"], "4.0.0")

    def crashed_read(*_: Any) -> Any:
        raise Crash()

    resolver.refusal_observers[key] = replace(resolver.refusal_observers[key], read=crashed_read)
    fallback(e)
    with pytest.raises(Crash):
        e.advance()
    assert e.job.result is None and "refusal" in e.job.evidence
    e.clm.replace_contract("C-1001", {"deal_value": 300000})
    e.clock.advance(seconds=31)
    service = restart(e)
    service.recover_interrupted()
    service._orch.advance(e.run_id)
    assert diagnostic(e).observations[0].fields[0].value == 300000
    assert diagnostic(e).observations[0].timing == "after_refusal"
    assert len(e.calls) == 1 and e.clm.operations.records() == []


def test_saved_diagnostic_is_immutable_and_refresh_never_reads_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path)
    fallback(e)
    e.execute()
    evidence = e.job.evidence
    monkeypatch.setattr(e.clm, "current_contract", lambda _: pytest.fail("refresh must not reread"))
    service = restart(e)
    for _ in range(2):
        service.get_run(ALICE, e.run_id)
        service.recover_interrupted()
        service._orch.advance(e.run_id)
    assert e.job.evidence == evidence
    with pytest.raises(JobConflictError, match="terminal evidence"):
        e.stores.maintenance.replace(
            e.job.model_copy(
                update={"revision": e.job.revision + 1, "evidence": {**evidence, "diagnostic": {}}}
            ),
            expected_revision=e.job.revision,
        )


@pytest.mark.parametrize(
    "seen",
    [
        observation(subject="other"),
        observation(resource="iam.user"),
        observation(timing="after_refusal"),
    ],
)
def test_foreign_or_mislabeled_guard_data_is_discarded(tmp_path: Path, seen: Any) -> None:
    e = Env(tmp_path)
    fallback(e, observations=(seen,))
    e.execute()
    assert e.job.evidence["refusal"]["observations"] == []
    assert diagnostic(e).observations[0].timing == "after_refusal"
    assert diagnostic(e).observations[0].subject == "C-1001"


def test_replay_guard_reports_partial_batch_and_zero_effects(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    impl = d.svc._orch._executor._tools["billing.replay_events"]

    def changed(args: Any, call: Any) -> Any:
        d.billing.change_event("ev-1001", {"amount": 999})
        return impl(args, call)

    d.svc._orch._executor._tools["billing.replay_events"] = changed
    view = d.run(replay_ids("ev-1001", "ev-1002"))
    job = next(
        j
        for j in d.stores.maintenance.for_run(OPERATOR.tenant_id, view.run_id)
        if j.kind == "write_readback"
    )
    saved = RefusalDiagnostic.model_validate(job.evidence["diagnostic"])
    assert saved.code == "event_changed" and saved.coverage == "partial"
    assert [o.subject for o in saved.observations] == ["ev-1001"]
    assert saved.observations[0].timing == "at_guard"
    assert d.billing.operations.records() == [] and d.billing.daily_total(OPERATOR.tenant_id) == 0


def test_later_replay_read_does_not_expose_a_record_moved_to_another_tenant(tmp_path: Path) -> None:
    d = Desk(tmp_path)

    def refused(args: Any, call: Any) -> Any:
        d.billing.change_event("ev-1001", {"tenant_id": "other", "customer": "secret"})
        raise ConnectorError("event_changed")

    d.svc._orch._executor._tools["billing.replay_events"] = refused
    view = d.run(replay_ids("ev-1001", "ev-1002"))
    job = next(
        j
        for j in d.stores.maintenance.for_run(OPERATOR.tenant_id, view.run_id)
        if j.kind == "write_readback"
    )
    saved = RefusalDiagnostic.model_validate(job.evidence["diagnostic"])
    assert saved.coverage == "complete" and saved.observations[0].status == "missing"
    assert saved.observations[1].status == "observed"
    assert all(o.timing == "after_refusal" for o in saved.observations)
    assert "secret" not in repr(job.evidence) and d.billing.operations.records() == []


def test_restore_refusal_keeps_its_guard_evidence_even_without_a_snapshot(tmp_path: Path) -> None:
    e = rated(tmp_path)
    e.clm.replace_contract("C-1001", {"deal_value": 150000})
    from tests.unit.test_trusted_versions import legacy_job

    job = legacy_job(e)
    e.stores.maintenance.add(job)
    run = e.service._undo.ensure(job)
    e.service._undo.process(job)
    step = e.stores.steps.get(run.run_id, "restore")
    assert "snapshots" not in step.resolved_args and step.error == "record_changed"
    job = next(
        j for j in e.stores.maintenance.for_run("t1", run.run_id) if j.kind == "write_readback"
    )
    saved = RefusalDiagnostic.model_validate(job.evidence["diagnostic"])
    assert saved.action_hash == step.action_hash and saved.observations[0].timing == "at_guard"
    assert e.stores.undo.for_run("t1", e.run_id)[0].state == "failed"


def test_broken_diagnostic_adapter_cannot_change_a_proven_refusal(tmp_path: Path) -> None:
    e = Env(tmp_path)
    resolver = e.orch._executor.verification
    assert resolver is not None
    key = (TOOLS["crm"], "4.0.0")
    resolver.refusal_observers[key] = replace(
        resolver.refusal_observers[key], subjects=lambda _: ()
    )
    fallback(e)
    e.execute()
    assert e.job.result == {"outcome": "not_dispatched", "code": "record_changed"}
    assert e.job.evidence["diagnostic_unavailable"] is True
    assert len(e.calls) == 1 and e.clm.operations.records() == []
