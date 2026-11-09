import json

import pytest

from gawe.core.models import Plan
from gawe.core.registry import ToolRegistry
from gawe.core.validator import (
    PlanValidationError,
    validate_plan,
    validate_raw_plan,
)


def step(sid: str, tool: str = "contracts.extract_risks", args: dict | None = None, deps=()):  # type: ignore[no-untyped-def]
    return {
        "id": sid,
        "tool": tool,
        "args": args if args is not None else {"document_id": "d1"},
        "depends_on": list(deps),
    }


def codes(result) -> set[str]:  # type: ignore[no-untyped-def]
    return {i.code for i in result.issues}


def check(registry: ToolRegistry, *steps: dict, **kwargs):  # type: ignore[no-untyped-def]
    return validate_raw_plan({"steps": list(steps)}, registry, **kwargs)


def test_valid_plan_with_ref(registry: ToolRegistry) -> None:
    result = check(
        registry,
        step("analyze"),
        step(
            "notify",
            "contracts.send_email",
            {"to": "legal@example.com", "body": {"$ref": "analyze.output.summary"}},
            ["analyze"],
        ),
    )
    assert result.ok, result.issues
    assert result.raise_if_invalid().steps[1].id == "notify"


def test_empty_plan(registry: ToolRegistry) -> None:
    assert codes(check(registry)) == {"EMPTY_PLAN"}


def test_too_many_steps(registry: ToolRegistry) -> None:
    result = check(registry, *[step(f"s{i}") for i in range(4)], max_steps=3)
    assert "TOO_MANY_STEPS" in codes(result)


def test_exactly_max_steps_is_ok(registry: ToolRegistry) -> None:
    assert check(registry, *[step(f"s{i}") for i in range(3)], max_steps=3).ok


def test_duplicate_ids(registry: ToolRegistry) -> None:
    assert "DUPLICATE_STEP_ID" in codes(check(registry, step("a"), step("a")))


def test_unknown_tool(registry: ToolRegistry) -> None:
    result = check(registry, step("a", "shell.exec", {}))
    assert codes(result) == {"UNKNOWN_TOOL"}


def test_unknown_dependency(registry: ToolRegistry) -> None:
    assert "UNKNOWN_DEPENDENCY" in codes(check(registry, step("a", deps=["ghost"])))


def test_self_dependency(registry: ToolRegistry) -> None:
    assert "SELF_DEPENDENCY" in codes(check(registry, step("a", deps=["a"])))


def test_cycle(registry: ToolRegistry) -> None:
    result = check(registry, step("a", deps=["b"]), step("b", deps=["a"]))
    assert codes(result) == {"CYCLE"}


def test_diamond_dag_is_fine(registry: ToolRegistry) -> None:
    result = check(
        registry,
        step("a"),
        step("b", deps=["a"]),
        step("c", deps=["a"]),
        step("d", deps=["b", "c"]),
    )
    assert result.ok, result.issues


def test_ref_to_non_dependency_rejected(registry: ToolRegistry) -> None:
    result = check(
        registry,
        step("a"),
        step("b", "contracts.send_email", {"to": "x@example.com", "body": {"$ref": "a.output.x"}}),
    )
    assert "REF_NOT_DEPENDENCY" in codes(result)


def test_ref_to_transitive_dependency_but_not_declared_is_rejected(registry: ToolRegistry) -> None:
    result = check(
        registry,
        step("a"),
        step("b", deps=["a"]),
        step(
            "c",
            "contracts.send_email",
            {"to": "x@example.com", "body": {"$ref": "a.output.x"}},
            ["b"],
        ),
    )
    assert "REF_NOT_DEPENDENCY" in codes(result)


@pytest.mark.parametrize(
    "ref", ["a", "a.input.x", "A.output.x", "a.output.", "../etc", "a.output.x y"]
)
def test_malformed_ref_strings(registry: ToolRegistry, ref: str) -> None:
    result = check(
        registry,
        step("a"),
        step("b", "contracts.send_email", {"to": "x@example.com", "body": {"$ref": ref}}, ["a"]),
    )
    assert "BAD_REF" in codes(result)


def test_nested_ref_rejected(registry: ToolRegistry) -> None:
    result = check(
        registry,
        step("a"),
        step(
            "b",
            "contracts.send_email",
            {"to": "x@example.com", "body": {"inner": {"$ref": "a.output.x"}}},
            ["a"],
        ),
    )
    assert "MALFORMED_REF" in codes(result)


def test_ref_with_extra_keys_rejected(registry: ToolRegistry) -> None:
    result = check(
        registry,
        step("a"),
        step(
            "b",
            "contracts.send_email",
            {"to": "x@example.com", "body": {"$ref": "a.output.x", "extra": 1}},
            ["a"],
        ),
    )
    assert "MALFORMED_REF" in codes(result)


def test_ref_for_unknown_arg_rejected(registry: ToolRegistry) -> None:
    result = check(
        registry,
        step("a"),
        step(
            "b",
            "contracts.send_email",
            {"to": "x@example.com", "body": "hi", "bcc": {"$ref": "a.output.x"}},
            ["a"],
        ),
    )
    assert "UNKNOWN_ARG" in codes(result)


def test_literal_arg_wrong_type(registry: ToolRegistry) -> None:
    result = check(registry, step("a", args={"document_id": 42}))
    assert codes(result) == {"ARG_SCHEMA"}


def test_missing_required_arg(registry: ToolRegistry) -> None:
    assert "ARG_SCHEMA" in codes(check(registry, step("a", args={})))


def test_extra_literal_arg_rejected_by_additional_properties_false(registry: ToolRegistry) -> None:
    result = check(registry, step("a", args={"document_id": "d", "sneaky": "x"}))
    assert "ARG_SCHEMA" in codes(result)


def test_ref_satisfies_required(registry: ToolRegistry) -> None:
    result = check(
        registry,
        step("a"),
        step("b", args={"document_id": {"$ref": "a.output.id"}}, deps=["a"]),
    )
    assert result.ok, result.issues


def test_long_error_messages_are_truncated(registry: ToolRegistry) -> None:
    result = check(registry, step("a", args={"document_id": "x" * 5000, "extra": "y" * 5000}))
    assert result.issues
    assert all(len(i.message) <= 200 for i in result.issues)


# --- untrusted parsing ---


def test_raw_json_string_accepted(registry: ToolRegistry) -> None:
    raw = json.dumps({"steps": [step("a")]})
    assert validate_raw_plan(raw, registry).ok


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        "null",
        {"steps": "nope"},
        {"steps": [{"id": "a"}]},
        {"steps": [step("Bad-Id")]},
        {"steps": [step("a")], "unexpected": 1},
        {"steps": [{**step("a"), "surprise": True}]},
    ],
)
def test_malformed_raw_plans_yield_schema_issues_not_exceptions(
    registry: ToolRegistry, raw: object
) -> None:
    result = validate_raw_plan(raw, registry)
    assert result.plan is None
    assert codes(result) == {"SCHEMA"}
    assert not result.ok


def test_raise_if_invalid(registry: ToolRegistry) -> None:
    with pytest.raises(PlanValidationError) as exc:
        check(registry, step("a", "shell.exec", {})).raise_if_invalid()
    assert exc.value.issues[0].code == "UNKNOWN_TOOL"


def test_validate_plan_accepts_parsed_plan(registry: ToolRegistry) -> None:
    plan = Plan.model_validate({"steps": [step("a")]})
    assert validate_plan(plan, registry).ok
