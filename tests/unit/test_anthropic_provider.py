import json
from types import SimpleNamespace
from typing import Any

import pytest

from gawe.llm.anthropic_provider import AnthropicProvider
from gawe.llm.provider import (
    LLMOutputError,
    LLMRequest,
    LLMUnavailableError,
    ProviderConfigError,
)

CLOSED = {
    "type": "object",
    "additionalProperties": False,
    "required": ["a"],
    "properties": {"a": {"type": "string", "maxLength": 5}},
}
OPEN = {"type": "object", "properties": {"args": {"type": "object", "additionalProperties": True}}}


def req(schema: dict[str, Any] = CLOSED) -> LLMRequest:
    return LLMRequest(
        purpose="read", system="sys", user="usr", output_schema=schema, max_output_tokens=321
    )


class FakeClient:
    def __init__(self, reply: Any = None, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._reply, self._error = reply, error
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._reply


def reply(text: str | None = '{"a": "x"}', stop: str = "end_turn") -> Any:
    blocks = [] if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(
        content=blocks,
        model="m-1",
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


def test_closed_schema_uses_structured_output_not_forced_tools() -> None:
    client = FakeClient(reply())
    response = AnthropicProvider(model="m-1", client=client).complete(req())
    assert json.loads(response.text) == {"a": "x"}
    assert (response.model, response.input_tokens, response.output_tokens) == ("m-1", 11, 7)
    call = client.calls[0]
    assert call["model"] == "m-1" and call["max_tokens"] == 321
    assert call["system"] == "sys" and call["messages"] == [{"role": "user", "content": "usr"}]
    assert "tools" not in call and "tool_choice" not in call  # rejected by recent models
    sent = call["extra_body"]["output_config"]["format"]
    assert sent["type"] == "json_schema"
    assert '"maxLength"' not in json.dumps(sent["schema"])  # the API rejects bounds
    assert "maxLength=5" in sent["schema"]["properties"]["a"]["description"]


def test_open_schema_falls_back_to_a_json_only_instruction() -> None:
    client = FakeClient(reply('{"args": {}}'))
    AnthropicProvider(model="m", client=client).complete(req(OPEN))
    call = client.calls[0]
    assert "extra_body" not in call
    assert call["system"].startswith("sys") and "ONLY a single JSON object" in call["system"]
    assert '"additionalProperties": true' in call["system"]


def test_a_code_fence_around_the_json_is_removed() -> None:
    client = FakeClient(reply('```json\n{"a": "x"}\n```'))
    out = AnthropicProvider(model="m", client=client).complete(req(OPEN))
    assert json.loads(out.text) == {"a": "x"}


@pytest.mark.parametrize(
    "bad", [reply(None), reply("{}", stop="refusal"), reply('{"a":', stop="max_tokens")]
)
def test_unusable_replies_are_output_errors(bad: Any) -> None:
    with pytest.raises(LLMOutputError):
        AnthropicProvider(model="m", client=FakeClient(bad)).complete(req())


def test_transient_errors_become_unavailable() -> None:
    class Net(Exception):
        pass

    client = FakeClient(error=Net("secret detail"))
    provider = AnthropicProvider(model="m", client=client, transient_errors=(Net,))
    with pytest.raises(LLMUnavailableError) as exc:
        provider.complete(req())
    assert "secret detail" not in str(exc.value)


def test_config_errors_are_explained_and_not_retried() -> None:
    class Rejected(Exception):
        message = "invalid x-api-key"

    provider = AnthropicProvider(
        model="m", client=FakeClient(error=Rejected()), config_errors=(Rejected,)
    )
    with pytest.raises(ProviderConfigError, match="ANTHROPIC_API_KEY") as exc:
        provider.complete(req())
    assert "invalid x-api-key" in str(exc.value)


def test_other_errors_propagate_unchanged() -> None:
    provider = AnthropicProvider(model="m", client=FakeClient(error=KeyError("bug")))
    with pytest.raises(KeyError):
        provider.complete(req())


def test_model_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GAWE_LLM_MODEL", raising=False)
    with pytest.raises(ProviderConfigError):
        AnthropicProvider(client=FakeClient())
    monkeypatch.setenv("GAWE_LLM_MODEL", "env-model")
    client = FakeClient(reply())
    AnthropicProvider(client=client).complete(req())
    assert client.calls[0]["model"] == "env-model"
