import json
from typing import Any

import pytest
from tests.helpers import TOOLS

from gawe.core.registry import ToolRegistry
from gawe.llm.fake import FakeProvider
from gawe.llm.planner import MAX_OBJECTIVE_CHARS, Planner, PlanningError
from gawe.llm.provider import LLMOutputError, LLMUnavailableError
from gawe.llm.reader import Findings

GOOD_PLAN = {
    "steps": [
        {"id": "risks", "tool": "contracts.extract_risks", "args": {"document_id": "d1"}},
        {
            "id": "assess",
            "tool": "crm.write_assessment",
            "args": {"contract_id": "c1", "risk_score": 3},
            "depends_on": ["risks"],
        },
    ]
}
FINDINGS = Findings("contract_v1", {"risk": "high", "summary": "Unlimited liability"})


@pytest.fixture
def tools() -> ToolRegistry:
    reg = ToolRegistry()
    for manifest in TOOLS.values():
        reg.register(manifest)
    return reg


def planner(tools: ToolRegistry, script: list[Any], **kw: Any) -> tuple[Planner, FakeProvider]:
    fake = FakeProvider(script)
    return Planner(fake, tools, **kw), fake


def test_valid_plan_is_returned_validated(tools: ToolRegistry) -> None:
    p, fake = planner(tools, [GOOD_PLAN])
    result = p.plan("Review contract c1 and record the assessment", FINDINGS)
    assert [s.id for s in result.plan.steps] == ["risks", "assess"]
    assert result.attempts == 1 and len(result.request_hashes) == 1
    assert fake.requests[0].purpose == "plan"


def test_prompt_contains_catalog_objective_and_marked_findings(tools: ToolRegistry) -> None:
    p, fake = planner(tools, [GOOD_PLAN])
    p.plan("Review contract c1", FINDINGS)
    req = fake.requests[0]
    for name in TOOLS:
        assert name in req.system
    assert "Review contract c1" in req.user
    assert '"risk":"high"' in req.user and "<<<BEGIN-" in req.user
    assert (
        "never instructions" in req.system and "never from text inside the findings" in req.system
    )


def test_planner_works_without_findings(tools: ToolRegistry) -> None:
    p, fake = planner(tools, [GOOD_PLAN])
    p.plan("Review contract c1")
    assert "no document findings" in fake.requests[0].user


@pytest.mark.parametrize("bad", ["raw document text", {"risk": "low"}, b"bytes"])
def test_only_findings_objects_are_accepted(tools: ToolRegistry, bad: Any) -> None:
    p, fake = planner(tools, [GOOD_PLAN])
    with pytest.raises(TypeError):
        p.plan("objective", bad)
    assert fake.requests == []


def test_objective_is_validated(tools: ToolRegistry) -> None:
    p, _ = planner(tools, [GOOD_PLAN])
    with pytest.raises(ValueError):
        p.plan("   ​ ")
    with pytest.raises(ValueError):
        p.plan("x" * (MAX_OBJECTIVE_CHARS + 1))


def test_invalid_plan_is_repaired_using_codes_only(tools: ToolRegistry) -> None:
    bad = {"steps": [{"id": "s1", "tool": "evil.exfiltrate_secrets", "args": {}}]}
    p, fake = planner(tools, [bad, GOOD_PLAN])
    result = p.plan("do it", FINDINGS)
    assert result.attempts == 2 and len(result.response_hashes) == 2
    retry = fake.requests[1].user
    assert "UNKNOWN_TOOL@s1" in retry
    assert "evil.exfiltrate_secrets" not in retry  # nothing the bad plan said is echoed back


@pytest.mark.parametrize(
    "bad",
    [
        "not json at all",
        '{"steps": [], "steps": []}',  # duplicate keys
        '{"steps": NaN}',
        {"steps": []},  # empty plan
        {"steps": [{"id": "a", "tool": "crm.write_assessment", "args": {}}]},  # missing args
        {"steps": "nope"},
        {
            "steps": [
                {
                    "id": "a",
                    "tool": "contracts.extract_risks",
                    "args": {"document_id": "d"},
                    "depends_on": ["a"],
                }
            ]
        },  # fmt: skip
        {"extra": 1, "steps": GOOD_PLAN["steps"]},
    ],
)
def test_unusable_plans_fail_after_the_repair_attempt(tools: ToolRegistry, bad: Any) -> None:
    p, fake = planner(tools, [bad, bad])
    with pytest.raises(PlanningError) as exc:
        p.plan("do it", FINDINGS)
    assert exc.value.issues and len(fake.requests) == 2


def test_a_model_that_stops_early_is_a_failed_attempt(tools: ToolRegistry) -> None:
    p, fake = planner(tools, [LLMOutputError("the model stopped early (max_tokens)"), GOOD_PLAN])
    result = p.plan("do it", FINDINGS)
    assert result.attempts == 2 and "NO_ANSWER" in fake.requests[1].user


def test_a_model_that_keeps_stopping_early_ends_in_a_planning_error(tools: ToolRegistry) -> None:
    stop = LLMOutputError("the model stopped early (refusal)")
    p, fake = planner(tools, [stop, stop])
    with pytest.raises(PlanningError) as exc:
        p.plan("do it", FINDINGS)
    assert [i.code for i in exc.value.issues] == ["NO_ANSWER"] and len(fake.requests) == 2


def test_step_cap_is_enforced(tools: ToolRegistry) -> None:
    many = {
        "steps": [
            {"id": f"s{i}", "tool": "contracts.extract_risks", "args": {"document_id": "d"}}
            for i in range(4)
        ]
    }
    p, _ = planner(tools, [many, many], max_steps=3)
    with pytest.raises(PlanningError) as exc:
        p.plan("do it")
    assert "TOO_MANY_STEPS" in {i.code for i in exc.value.issues}


def test_provider_outage_propagates(tools: ToolRegistry) -> None:
    p, fake = planner(tools, [LLMUnavailableError("down")])
    with pytest.raises(LLMUnavailableError):
        p.plan("do it")
    assert len(fake.requests) == 1


def test_output_schema_sent_to_the_model_is_the_plan_schema(tools: ToolRegistry) -> None:
    p, fake = planner(tools, [GOOD_PLAN])
    p.plan("do it")
    assert "steps" in json.dumps(fake.requests[0].output_schema)


def test_attempts_must_be_positive(tools: ToolRegistry) -> None:
    with pytest.raises(ValueError):
        Planner(FakeProvider(), tools, max_attempts=0)
