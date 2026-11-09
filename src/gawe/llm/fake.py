"""A scripted provider for deterministic tests. It never makes a network call."""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from gawe.llm.provider import LLMRequest, LLMResponse

Scripted = str | Mapping[str, Any] | Sequence[Any] | BaseException | Callable[[LLMRequest], Any]


class FakeProvider:
    """Returns the scripted items in order, one per call.

    An item can be raw text, a JSON-serializable value (sent as JSON text), an exception (raised),
    or a callable that receives the request and returns text or a JSON-serializable value.
    Running out of script is a test bug and raises AssertionError.
    """

    def __init__(self, script: Sequence[Scripted] = (), *, model: str = "fake-model") -> None:
        self._script = list(script)
        self._model = model
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("FakeProvider script exhausted")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(request)
        text = item if isinstance(item, str) else json.dumps(item)
        return LLMResponse(text=text, model=self._model, input_tokens=10, output_tokens=5)
