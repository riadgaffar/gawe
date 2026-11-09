import pytest

from gawe.core.canonical import CanonicalizationError, action_hash, canonical_json, sha256_hex

BASE = {
    "tool": "contracts.send_email",
    "version": "1.0.0",
    "resolved_args": {"to": "a@example.com", "body": "hi"},
    "run_id": "run1",
    "step_id": "s1",
}


def test_key_order_does_not_matter() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_nested_key_order_does_not_matter() -> None:
    assert canonical_json({"x": {"b": 1, "a": [1, {"d": 1, "c": 2}]}}) == canonical_json(
        {"x": {"a": [1, {"c": 2, "d": 1}], "b": 1}}
    )


def test_whole_floats_equal_ints() -> None:
    assert canonical_json({"v": 1.0}) == canonical_json({"v": 1})


def test_bool_and_int_are_distinct() -> None:
    assert canonical_json({"v": True}) != canonical_json({"v": 1})


def test_non_ascii_is_stable() -> None:
    assert canonical_json({"v": "café"}) == '{"v":"café"}'.encode()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1: "x"}, {"v": {1, 2}}, b"bytes"])
def test_unrepresentable_values_rejected(bad: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(bad)


def test_action_hash_is_deterministic() -> None:
    assert action_hash(**BASE) == action_hash(**BASE)
    assert len(action_hash(**BASE)) == 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("tool", "contracts.other"),
        ("version", "1.0.1"),
        ("resolved_args", {"to": "evil@example.com", "body": "hi"}),
        ("run_id", "run2"),
        ("step_id", "s2"),
    ],
)
def test_action_hash_changes_when_anything_changes(field: str, value: object) -> None:
    changed = {**BASE, field: value}
    assert action_hash(**changed) != action_hash(**BASE)  # type: ignore[arg-type]


def test_action_hash_is_domain_separated() -> None:
    payload = canonical_json(
        {
            "tool": BASE["tool"],
            "version": BASE["version"],
            "args": BASE["resolved_args"],
            "run_id": BASE["run_id"],
            "step_id": BASE["step_id"],
        }
    )
    assert action_hash(**BASE) != sha256_hex(payload)  # type: ignore[arg-type]
