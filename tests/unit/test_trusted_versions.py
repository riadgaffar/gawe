"""M27: newer manifests cannot change durable undo requests, plans, approvals or keys."""

from datetime import timedelta
from pathlib import Path

import pytest
from tests.engine_flow import INTERNAL_PLAN, EngineFlow
from tests.unit.test_undo_runs import approve, rated
from tests.unit.test_write_readback import ALICE

from gawe.core.canonical import action_hash
from gawe.core.maintenance import JobConflictError, MaintenanceJob
from gawe.core.models import Plan, PlanStep
from gawe.core.records import StepNotFoundError
from gawe.core.undo import reversal_key, reversal_run_id
from gawe.core.validator import validate_plan, validate_raw_plan


def test_model_cannot_pin_even_current_version_or_null() -> None:
    f = EngineFlow([])
    for version in ["1.0.0", "0.0.0", None]:
        raw = {
            "steps": [
                {
                    "id": "read",
                    "tool": "contracts.extract_risks",
                    "args": {"document_id": "d"},
                    "tool_version": version,
                }
            ]
        }
        result = validate_raw_plan(raw, f.registry)
        assert not result.ok and "TOOL_VERSION" in {i.code for i in result.issues}
        assert not validate_plan(Plan.model_validate(raw), f.registry).ok


def test_old_plan_json_and_model_output_schema_do_not_gain_a_pin() -> None:
    old = {
        "steps": [
            {
                "id": "read",
                "tool": "contracts.extract_risks",
                "args": {"document_id": "d"},
                "depends_on": [],
            }
        ]
    }
    assert Plan.model_validate(old).model_dump(mode="json") == old
    f = EngineFlow([INTERNAL_PLAN])
    f.start()
    f.orch.advance("run1")
    schema = f.planner_llm.requests[0].output_schema
    assert "tool_version" not in str(schema)


def legacy_job(e):  # type: ignore[no-untyped-def]
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    run = e.stores.runs.get(e.run_id)
    return MaintenanceJob(
        job_id=reversal_key(record.operation_key),
        kind="undo_request",
        tenant_id="t1",
        run_id=e.run_id,
        plan_version=run.plan_version,
        step_id="write",
        action_hash=record.action_hash,
        operation_key=reversal_key(record.operation_key),
        created_at=e.clock.now,
        due_at=e.clock.now,
        deadline=e.clock.now + timedelta(days=36500),
        payload={
            "undo_id": record.undo_id,
            "user_id": "alice",
            "roles": ["case_manager"],
            "tool": "crm.restore_assessment",
        },
    )


@pytest.mark.parametrize("boundary", ["before_run", "before_materialize", "approval"])
def test_legacy_undo_keeps_version_and_hash_after_registry_upgrade(
    tmp_path: Path, boundary: str
) -> None:
    from gawe.core.undo import restore_args

    e = rated(tmp_path, "low" if boundary == "approval" else "high")
    undo = e.service._undo
    assert undo is not None
    record = e.stores.undo.for_run("t1", e.run_id)[0]
    job = legacy_job(e)
    e.stores.maintenance.add(job)
    run_id = reversal_run_id(record)
    args = restore_args(record)
    expected_hash = action_hash(
        tool="crm.restore_assessment",
        version="1.0.0",
        resolved_args=args,
        run_id=run_id,
        step_id="restore",
    )
    if boundary != "before_run":
        e.orch.start_run(
            tenant_id="t1",
            acting_user_id="alice",
            roles=("case_manager",),
            objective="undo",
            profile="undo",
            run_id=run_id,
        )
        e.stores.runs.update(
            run_id,
            plan_version=1,
            plan=Plan(steps=(PlanStep(id="restore", tool="crm.restore_assessment", args=args),)),
        )
        undo.ensure(job)
        if boundary == "approval":
            e.orch.advance(run_id)
            assert e.stores.steps.get(run_id, "restore").action_hash == expected_hash
    manifest = undo.r.registry.get("crm.restore_assessment")
    undo.r.registry.register(manifest.model_copy(update={"version": "3.0.0"}))
    undo.process(job)
    if boundary == "approval":
        approve(e, run_id)
    step = e.stores.steps.get(run_id, "restore")
    assert step.tool_version == "1.0.0" and step.action_hash == expected_hash
    assert e.stores.undo.for_run("t1", e.run_id)[0].state == "used"
    assert (
        len([r for r in e.clm.operations.records() if r["operation_key"] == job.operation_key]) == 1
    )
    assert undo.r.operation_key(step) == job.operation_key
    with pytest.raises(JobConflictError, match="version"):
        undo.r.operation_key(step.model_copy(update={"tool_version": "2.0.0"}))


def test_new_request_persists_chosen_version_before_any_future_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = rated(tmp_path)
    undo = e.service._undo
    assert undo is not None
    manifest = undo.r.registry.get("crm.restore_assessment")
    monkeypatch.setattr(e.service, "kick", lambda _: None)
    run = e.service.request_undo(ALICE, e.run_id, "write")
    job = next(j for j in e.stores.maintenance.for_run("t1", e.run_id) if j.kind == "undo_request")
    assert job.payload["tool_version"] == "2.0.0"
    before = e.stores.runs.get(run.run_id).plan
    assert before.steps[0].tool_version == "2.0.0"
    undo.r.registry.register(manifest.model_copy(update={"version": "3.0.0"}))
    undo.ensure(job)
    assert e.stores.runs.get(run.run_id).plan == before


def test_untrusted_run_cannot_materialize_an_internal_version_pin() -> None:
    f = EngineFlow([])
    run = f.start()
    plan = Plan(
        steps=(
            PlanStep(
                id="read",
                tool="contracts.extract_risks",
                tool_version="1.0.0",
                args={"document_id": "d"},
            ),
        )
    )
    run = f.runs.update(run.run_id, plan=plan, plan_version=1)
    with pytest.raises(ValueError, match="trusted undo"):
        f.orch._materialize(run)
    with pytest.raises(StepNotFoundError):
        f.steps.get(run.run_id, "read")


def test_unknown_legacy_tool_or_unregistered_pin_never_falls_back(tmp_path: Path) -> None:
    from gawe.core.registry import RegistryError
    from gawe.core.undo import legacy_restore_version

    with pytest.raises(ValueError, match="no legacy version"):
        legacy_restore_version("another.restore")
    e = rated(tmp_path)
    undo = e.service._undo
    assert undo is not None
    job = legacy_job(e)
    job = job.model_copy(update={"payload": {**job.payload, "tool_version": "9.0.0"}})
    with pytest.raises(RegistryError):
        undo.ensure(job)
    assert e.stores.undo.for_run("t1", e.run_id)[0].state == "armed"
