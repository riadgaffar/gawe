"""The same durable intent/CAS/tenant/continuation contract on both backends."""

from datetime import timedelta
from typing import Any

import pytest

from gawe.core.maintenance import (
    InMemoryMaintenanceStore,
    JobConflictError,
    MaintenanceJob,
    MaintenanceStore,
)
from tests.contract.conftest import TENANT, Backend
from tests.flow import T0


@pytest.fixture
def jobs(backend: Backend) -> MaintenanceStore:
    if backend.name == "memory":
        return InMemoryMaintenanceStore()
    from gawe.db.maintenance_store import PostgresMaintenanceStore

    return PostgresMaintenanceStore(backend.steps._bind)  # type: ignore[attr-defined]


def make_job(backend: Backend, **updates: Any) -> MaintenanceJob:
    return MaintenanceJob(
        **{
            "job_id": "a" * 64,
            "kind": "write_readback",
            "tenant_id": TENANT,
            "run_id": backend.run_id,
            "plan_version": 1,
            "step_id": "s1",
            "action_hash": "b" * 64,
            "operation_key": "c" * 64,
            "created_at": T0,
            "deadline": T0 + timedelta(minutes=5),
            "due_at": T0,
            **updates,
        }
    )


def test_insert_is_idempotent_and_reads_are_tenant_scoped(
    jobs: MaintenanceStore, backend: Backend
) -> None:
    job = make_job(backend)
    assert jobs.add(job) and not jobs.add(job)
    assert jobs.get(TENANT, job.job_id) == job
    assert jobs.get("00000000-0000-0000-0000-0000000000b1", job.job_id) is None
    assert jobs.for_run(TENANT, backend.run_id) == [job]
    assert jobs.for_run("00000000-0000-0000-0000-0000000000b1", backend.run_id) == []


def test_due_honors_leases_and_pending_continuation(
    jobs: MaintenanceStore, backend: Backend
) -> None:
    job = make_job(backend, lease_owner="one", lease_until=T0 + timedelta(seconds=30))
    jobs.add(job)
    assert jobs.due(T0) == []
    assert jobs.due(T0 + timedelta(seconds=30)) == [job]
    completed = job.model_copy(
        update={
            "revision": 1,
            "lease_owner": None,
            "lease_until": None,
            "result": {"outcome": "agreement"},
        }
    )
    jobs.replace(completed, expected_revision=0)
    assert jobs.due(T0) == [completed]  # still needs step/run continuation
    jobs.replace(
        completed.model_copy(update={"revision": 2, "continued": True}), expected_revision=1
    )
    assert jobs.due(T0) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("deadline", T0 + timedelta(minutes=6)),
        ("operation_key", "d" * 64),
        ("plan_version", 2),
        ("step_id", "another"),
        ("action_hash", "d" * 64),
        ("payload", {"changed": True}),
    ],
)
def test_intent_identity_and_deadline_cannot_move(
    jobs: MaintenanceStore,
    backend: Backend,
    field: str,
    value: Any,
) -> None:
    job = make_job(backend)
    jobs.add(job)
    with pytest.raises(JobConflictError):
        jobs.replace(job.model_copy(update={field: value, "revision": 1}), expected_revision=0)


def test_stale_revision_and_terminal_result_are_fenced(
    jobs: MaintenanceStore, backend: Backend
) -> None:
    job = make_job(backend)
    jobs.add(job)
    completed = job.model_copy(update={"revision": 1, "result": {"outcome": "unknown"}})
    jobs.replace(completed, expected_revision=0)
    with pytest.raises(JobConflictError):
        jobs.replace(completed, expected_revision=0)
    with pytest.raises(JobConflictError):
        jobs.replace(
            completed.model_copy(update={"revision": 2, "result": {"outcome": "agreement"}}),
            expected_revision=1,
        )


def test_same_text_step_in_new_plan_has_distinct_job(
    jobs: MaintenanceStore, backend: Backend
) -> None:
    first = make_job(backend)
    second = make_job(backend, job_id="d" * 64, plan_version=2, operation_key="e" * 64)
    assert jobs.add(first) and jobs.add(second)
    assert len(jobs.for_run(TENANT, backend.run_id)) == 2


def test_due_cursor_pages_past_earlier_jobs_and_counts_keep_terminal_outcomes(
    jobs: MaintenanceStore, backend: Backend
) -> None:
    made = [
        make_job(backend, job_id=f"{i:x}" * 64, due_at=T0 + timedelta(seconds=i)) for i in range(4)
    ]
    for job in made:
        jobs.add(job)
    first = jobs.due(T0 + timedelta(minutes=1), limit=2)
    assert [j.job_id for j in first] == [made[0].job_id, made[1].job_id]
    rest = jobs.due(T0 + timedelta(minutes=1), limit=5, after=(first[-1].due_at, first[-1].job_id))
    assert [j.job_id for j in rest] == [made[2].job_id, made[3].job_id]
    unknown = made[0].model_copy(
        update={"revision": 1, "result": {"outcome": "unknown", "code": "verification_deadline"}}
    )
    jobs.replace(unknown, expected_revision=0)
    jobs.replace(unknown.model_copy(update={"revision": 2, "continued": True}), expected_revision=1)
    assert jobs.counts("write_readback") == {"unresolved": 3, "unknown": 1}
    assert jobs.counts("another_kind") == {}


def test_snapshot_and_diagnostic_use_existing_json_storage(
    jobs: MaintenanceStore, backend: Backend
) -> None:
    from gawe.core.record_changes import RecordSnapshot, RefusalDiagnostic
    from gawe.core.records import StepRecord
    from tests.unit.test_record_changes import observation, snapshot

    captured = snapshot()
    backend.steps.add(
        StepRecord(
            run_id=backend.run_id,
            step_id="s1",
            tool="crm.write_assessment",
            tool_version="3.0.0",
            resolved_args={"record": captured.model_dump(mode="json")},
        )
    )
    step = backend.steps.get(backend.run_id, "s1")
    assert RecordSnapshot.model_validate(step.resolved_args["record"]) == captured
    diagnostic = RefusalDiagnostic(
        action_hash="b" * 64,
        code="record_changed",
        coverage="complete",
        observations=(observation(timing="after_refusal"),),
    )
    job = make_job(backend)
    jobs.add(job)
    jobs.replace(
        job.model_copy(
            update={
                "revision": 1,
                "result": {"outcome": "refused", "diagnostic": diagnostic.model_dump(mode="json")},
            }
        ),
        expected_revision=0,
    )
    loaded = jobs.get(TENANT, job.job_id)
    assert loaded is not None and loaded.result is not None
    reloaded = RefusalDiagnostic.model_validate(loaded.result["diagnostic"])
    assert reloaded == diagnostic
    reloaded.validate_for(step.action_hash or "b" * 64, {("clm.contract", "C-1001")})


def test_refusal_proof_survives_reload_and_fences_outcomes(
    jobs: MaintenanceStore, backend: Backend
) -> None:
    job = make_job(backend)
    jobs.add(job)
    proof = {"code": "record_changed", "observations": []}
    saved = job.model_copy(update={"revision": 1, "evidence": {"refusal": proof}})
    jobs.replace(saved, expected_revision=0)
    assert jobs.get(TENANT, job.job_id).evidence == {"refusal": proof}
    for changes in [
        {"evidence": {}},
        {"result": {"outcome": "agreement", "code": "readback_agreed"}},
    ]:
        with pytest.raises(JobConflictError, match="refusal"):
            jobs.replace(saved.model_copy(update={"revision": 2, **changes}), expected_revision=1)
    terminal = saved.model_copy(
        update={
            "revision": 2,
            "evidence": {"refusal": proof, "diagnostic_unavailable": True},
            "result": {"outcome": "not_dispatched", "code": "record_changed"},
        }
    )
    jobs.replace(terminal, expected_revision=1)
    assert jobs.get(TENANT, job.job_id) == terminal
