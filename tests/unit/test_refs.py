import pytest

from gawe.core.refs import RefResolutionError, resolve_args

OUT = {"risks": {"score": 7, "items": [{"id": "a"}, {"id": "b"}], "nested": {"k": "v"}}}


def test_literals_pass_through_and_refs_resolve() -> None:
    args = {"x": 1, "y": {"$ref": "risks.output.score"}, "z": {"$ref": "risks.output.nested.k"}}
    assert resolve_args(args, OUT) == {"x": 1, "y": 7, "z": "v"}


def test_list_index_paths_resolve() -> None:
    assert resolve_args({"a": {"$ref": "risks.output.items.1.id"}}, OUT) == {"a": "b"}


def test_whole_output_reference() -> None:
    assert resolve_args({"a": {"$ref": "risks.output"}}, OUT)["a"]["score"] == 7


@pytest.mark.parametrize(
    "ref",
    ["risks.output.nope", "risks.output.items.9", "risks.output.items.x", "other.output.score"],
)
def test_unresolvable_references_raise(ref: str) -> None:
    with pytest.raises(RefResolutionError):
        resolve_args({"a": {"$ref": ref}}, OUT)


def test_missing_output_raises_without_leaking_values() -> None:
    with pytest.raises(RefResolutionError) as exc:
        resolve_args({"a": {"$ref": "risks.output.score"}}, {"risks": None})
    assert "7" not in str(exc.value)


def test_template_is_not_mutated() -> None:
    template = {"a": {"$ref": "risks.output.score"}}
    resolve_args(template, OUT)
    assert template == {"a": {"$ref": "risks.output.score"}}
