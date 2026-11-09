"""Benchmark tests never reach a real model, not even the free token counter.

Any attempt to build the Anthropic provider or SDK client fails the test at once, so a harness
bug cannot spend money or leak fixture text from `make check`.
"""

import importlib
from typing import Any

import pytest

from gawe.llm import anthropic_provider


class RealModelUsedError(AssertionError):
    pass


def _refuse(*args: Any, **kwargs: Any) -> None:
    raise RealModelUsedError("benchmark tests must not construct a real model client")


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anthropic_provider.AnthropicProvider, "__init__", _refuse)
    try:
        sdk = importlib.import_module("anthropic")
    except ImportError:
        return
    for name in ("Anthropic", "AsyncAnthropic"):
        client = getattr(sdk, name, None)
        if client is not None:
            monkeypatch.setattr(client, "__init__", _refuse)
