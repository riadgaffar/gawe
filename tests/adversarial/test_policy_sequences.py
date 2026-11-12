"""Allowed actions and attacks share real app closures, stores and connector state."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from hypothesis import example, given, seed, settings
from hypothesis import strategies as st

from bench.injection.cases import Case, expected_scripted, load_suite
from bench.injection.run import ROLES, TENANT, Recorder, TrialEnvironment, classify, drive
from bench.injection.scripted import (
    ScriptedModel,
    ScriptedTrial,
    metered,
    replies_for,
    scripted_provider,
    scripted_trials,
)
from gawe.api.auth import Identity
from gawe.core.runs import RunRecord
from gawe.core.statemachine import RunStatus, StepStatus

TRIALS = scripted_trials(load_suite())
PLANNER_TRIALS = [trial for trial in TRIALS if trial.mode == "planner"]
PREFIXES = st.lists(st.integers(min_value=0, max_value=5), min_size=0, max_size=6)


def no_sleep(seconds: float) -> None:
    raise AssertionError("scripted runs must never wait")


def probe(case: Case, findings: dict[str, Any]) -> RunRecord:
    return RunRecord(
        run_id="probe",
        tenant_id=TENANT,
        acting_user_id="requester",
        objective="Fixed findings",
        profile=case.app,
        created_at=datetime(2026, 11, 12, tzinfo=UTC),
        findings=findings,
    )


def values(env: TrialEnvironment, fixed: RunRecord) -> dict[str, Any]:
    # Deep copy: an in-place mutation of a callback's list must not mutate the expected snapshot.
    return deepcopy(dict(env.bundle.variables(fixed)))


def assert_stable(env: TrialEnvironment, fixed: RunRecord, before: dict[str, Any]) -> None:
    assert values(env, fixed) == before, "policy variable drift after allowed prefix"


def prefix_steps(case: Case, codes: list[int]) -> list[dict[str, Any]]:
    if case.app == "contract_review":
        actions = [
            ("clm.get_contract", {"contract_id": "C-1001"}),
            ("clm.get_contract", {"contract_id": "C-1002"}),
            (
                "crm.write_assessment",
                {"contract_id": "C-1001", "risk_level": "medium", "summary": "Honest review"},
            ),
            (
                "crm.write_assessment",
                {"contract_id": "C-1002", "risk_level": "high", "summary": "Honest review"},
            ),
            (
                "email.send",
                {"to": "legal@acme.example", "subject": "Review", "body": "Please review"},
            ),
            (
                "email.send",
                {"to": "ops@acme.example", "subject": "Update", "body": "Review recorded"},
            ),
        ]
    else:
        email = case.base.clean_findings["requester_email"]
        actions = [
            ("iam.lookup_user", {"email": email}),
            ("iam.lookup_user", {"email": "sam.okafor@acme.example"}),
            ("iam.grant_access", {"email": email, "system": "wiki", "level": "read"}),
            ("iam.grant_access", {"email": email, "system": "wiki", "level": "write"}),
            ("iam.grant_access", {"email": email, "system": "crm", "level": "read"}),
            ("iam.grant_access", {"email": email, "system": "billing", "level": "write"}),
        ]
    return [
        {
            "id": f"prefix_{i}",
            "tool": actions[code][0],
            "args": deepcopy(actions[code][1]),
            "depends_on": [f"prefix_{i - 1}"] if i else [],
        }
        for i, code in enumerate(codes)
    ]


def execute_plan(env, case, findings, plan):
    recorder = Recorder(metered(ScriptedModel(findings, plan)))
    service = env.service(recorder)
    who = Identity(TENANT, "requester", ROLES[case.app])
    run_id = service.start_run(
        who,
        objective="Execute the synthetic test plan",
        profile=case.app,
        document="Synthetic document",
    ).run_id
    paused = env.stores.runs.get(run_id).status is RunStatus.PAUSED_FOR_APPROVAL
    if paused:
        service.cancel_run(who, run_id)
    return env.stores.runs.get(run_id), recorder, paused


def assert_prefix_completed(env, run, steps):
    keys = []
    for step in steps:
        assert run.decisions[step["id"]].outcome == "allow"
        assert env.stores.steps.get(run.run_id, step["id"]).status is StepStatus.SUCCEEDED
        attempts = env.stores.executions.attempts(run.run_id, step["id"])
        assert len(attempts) == 1 and attempts[0].status == "succeeded"
        keys.append(attempts[0].idempotency_key)
    assert len(set(keys)) == len(steps)


def run_prefix(env, case, steps):
    if not steps:
        return None  # Zero actions means no prefix run; the engine rejects empty plans.
    run, _, paused = execute_plan(env, case, case.base.clean_findings, {"steps": steps})
    assert not paused and run.status is RunStatus.COMPLETED
    assert_prefix_completed(env, run, steps)
    return run


def assert_expected(result, trial):
    expected = expected_scripted(trial.case, trial.mode)
    assert result["status"] == "ok"
    assert result["stopped_by"] == expected
    assert result["unauthorized_step_ran"] == (expected == "none")


@pytest.mark.parametrize("trial", TRIALS, ids=lambda t: f"{t.case.case_id}-{t.mode}")
@seed(15004)
@settings(max_examples=10, derandomize=True, database=None, deadline=None)
@example(codes=[])
@example(codes=[0, 1, 2, 3, 4, 5])
@given(codes=PREFIXES)
def test_all_scripted_attacks_after_shared_state_prefixes(
    trial: ScriptedTrial, codes: list[int]
) -> None:
    with TemporaryDirectory(prefix="gawe-sequence-") as tmp:
        env = TrialEnvironment.create(trial.case.app, Path(tmp))
        fixed = probe(trial.case, replies_for(trial.case, trial.mode)[0])
        before = values(env, fixed)
        prefix_run = run_prefix(env, trial.case, prefix_steps(trial.case, codes))
        assert_stable(env, fixed, before)
        prefix_effects = env.effects()
        result = drive(
            trial.case, scripted_provider(trial.case, trial.mode), no_sleep, environment=env
        )
        assert_expected(result, trial)
        assert_stable(env, fixed, before)
        if prefix_run is not None:
            assert env.stores.runs.get(prefix_run.run_id) == prefix_run
        assert len(env.stores.runs.list_for_tenant(TENANT)) == 1 + bool(codes)
        assert env.effects()[: len(prefix_effects)] == prefix_effects
        keys = [effect["key"] for effect in env.effects()]
        assert len(keys) == len(set(keys))


@pytest.mark.parametrize("trial", PLANNER_TRIALS, ids=lambda t: t.case.case_id)
@seed(15005)
@settings(max_examples=10, derandomize=True, database=None, deadline=None)
@example(codes=[])
@example(codes=[0, 1, 2, 3, 4, 5])
@given(codes=PREFIXES)
def test_planner_attacks_after_prefixes_in_the_same_run(
    trial: ScriptedTrial, codes: list[int]
) -> None:
    with TemporaryDirectory(prefix="gawe-within-run-") as tmp:
        env = TrialEnvironment.create(trial.case.app, Path(tmp))
        findings, plan = replies_for(trial.case, trial.mode)
        fixed = probe(trial.case, findings)
        before = values(env, fixed)
        steps = prefix_steps(trial.case, codes)
        boundary = {"offset": 0, "values": before, "calls": 0}
        if steps:
            # Observe the actual boundary after the last prefix tool returns, before the attack.
            last = steps[-1]
            original = env.bundle.tools[last["tool"]]

            def observe(args, call):
                result = original(args, call)
                if call.step_id == last["id"]:
                    boundary.update(offset=len(env.effects()), values=values(env, fixed), calls=1)
                return result

            env = replace(
                env, bundle=replace(env.bundle, tools={**env.bundle.tools, last["tool"]: observe})
            )
            for step in plan["steps"]:
                if not step.get("depends_on"):
                    step["depends_on"] = [last["id"]]
        plan["steps"] = steps + plan["steps"]
        run, recorder, paused = execute_plan(env, trial.case, findings, plan)
        assert_prefix_completed(env, run, steps)
        assert boundary["calls"] == bool(steps)
        assert boundary["values"] == before, "policy variable drift at within-run boundary"
        assert_stable(env, fixed, before)
        result = classify(
            trial.case, run, recorder.replies, env.effects()[boundary["offset"] :], paused=paused
        )
        assert_expected(result, trial)


def test_prefix_effect_matching_goal_is_not_counted_as_attack_success(tmp_path: Path) -> None:
    trial = next(t for t in TRIALS if t.case.goal.name == "lateral_grant" and t.mode == "planner")
    env = TrialEnvironment.create(trial.case.app, tmp_path)
    findings = {**trial.case.base.clean_findings, "requester_email": "sam.okafor@acme.example"}
    step = {
        "id": "legitimate",
        "tool": "iam.grant_access",
        "args": {"email": "sam.okafor@acme.example", "system": "billing", "level": "write"},
    }
    run, _, _ = execute_plan(env, trial.case, findings, {"steps": [step]})
    assert run.status is RunStatus.COMPLETED
    assert_prefix_completed(env, run, [step])
    prefix = env.effects()
    result = drive(trial.case, scripted_provider(trial.case, trial.mode), no_sleep, environment=env)
    assert_expected(result, trial)
    assert not result["unauthorized_step_ran"]
    assert env.effects()[: len(prefix)] == prefix
    # The old all-ledger classifier would incorrectly count the earlier legitimate grant.
    attack_run = env.stores.runs.list_for_tenant(TENANT)[0]
    contaminated = classify(trial.case, attack_run, {}, env.effects(), paused=False)
    assert contaminated["unauthorized_step_ran"]


def test_false_source_declaration_is_detected_by_shared_state_tripwire(tmp_path: Path) -> None:
    trial = next(t for t in TRIALS if t.case.goal.name == "downgrade" and t.mode == "reader")
    env = TrialEnvironment.create(trial.case.app, tmp_path)
    state = {"written": False}
    original_tool = env.bundle.tools["crm.write_assessment"]
    original_variables = env.bundle.variables

    def write(args, call):
        result = original_tool(args, call)
        state["written"] = True
        return result

    def variables(run):
        result = deepcopy(dict(original_variables(run)))
        if state["written"]:
            result["review"]["large_deals"] = []
        return result

    # Deliberately lie: declarations still claim only CRM writes and a CLM-backed variable.
    env = replace(
        env,
        bundle=replace(
            env.bundle,
            tools={**env.bundle.tools, "crm.write_assessment": write},
            variables=variables,
        ),
    )
    fixed = probe(trial.case, replies_for(trial.case, trial.mode)[0])
    before = values(env, fixed)
    baseline = drive(
        trial.case, scripted_provider(trial.case, trial.mode), no_sleep, environment=env
    )
    assert_expected(baseline, trial)
    run_prefix(env, trial.case, prefix_steps(trial.case, [3]))
    with pytest.raises(AssertionError, match="policy variable drift"):
        assert_stable(env, fixed, before)
    weakened = drive(
        trial.case, scripted_provider(trial.case, trial.mode), no_sleep, environment=env
    )
    assert weakened["stopped_by"] == "none" and weakened["unauthorized_step_ran"]
    with pytest.raises(AssertionError):
        assert_expected(weakened, trial)


def test_sequence_coverage_tracks_every_scripted_case_mode() -> None:
    assert len(TRIALS) == 38 and len(PLANNER_TRIALS) == 26
    assert len({(t.case.case_id, t.mode) for t in TRIALS}) == 38
    assert {t.case.goal.name for t in TRIALS} == set(load_suite().goals)


def test_environment_cannot_be_used_for_another_app(tmp_path: Path) -> None:
    trial = next(t for t in TRIALS if t.case.app == "it_access")
    env = TrialEnvironment.create("contract_review", tmp_path)
    with pytest.raises(ValueError, match="different app"):
        drive(trial.case, scripted_provider(trial.case, trial.mode), no_sleep, environment=env)
