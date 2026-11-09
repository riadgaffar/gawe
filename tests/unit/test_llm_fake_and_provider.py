import json

import pytest

from gawe.llm.fake import FakeProvider
from gawe.llm.provider import LLMRequest, LLMUnavailableError, request_hash, response_hash

REQ = LLMRequest(purpose="read", system="s", user="u", output_schema={"type": "object"})


def test_scripted_items_are_returned_in_order() -> None:
    fake = FakeProvider(["raw text", {"a": 1}, lambda r: {"echo": r.user}])
    assert fake.complete(REQ).text == "raw text"
    assert json.loads(fake.complete(REQ).text) == {"a": 1}
    assert json.loads(fake.complete(REQ).text) == {"echo": "u"}
    assert len(fake.requests) == 3


def test_scripted_exceptions_are_raised() -> None:
    fake = FakeProvider([LLMUnavailableError("down")])
    with pytest.raises(LLMUnavailableError):
        fake.complete(REQ)


def test_running_out_of_script_is_loud() -> None:
    with pytest.raises(AssertionError, match="exhausted"):
        FakeProvider().complete(REQ)


def test_request_has_no_tools_field() -> None:
    assert not hasattr(REQ, "tools")


def test_hashes_are_stable_and_sensitive() -> None:
    other = LLMRequest(purpose="read", system="s", user="u2", output_schema={"type": "object"})
    assert request_hash(REQ) == request_hash(REQ)
    assert request_hash(REQ) != request_hash(other)
    resp = FakeProvider(["x"]).complete(REQ)
    assert response_hash(resp) == response_hash(resp) and len(response_hash(resp)) == 64
