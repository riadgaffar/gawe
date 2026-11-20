"""Exact input token counts before anything is spent.

The requests are not rebuilt here: the real Reader and Planner are run against a stand-in
provider that records the request they send and stops. So a count covers exactly the
instructions, fence, document and schema a live call would carry. Counting uses the Messages
API's token-counting endpoint, which is free (it has its own rate limits) and shares its request
builder with the provider (`build_request_kwargs`).

A Planner request depends on what the Reader found, which is unknown in advance; it is counted
with the largest findings the app's Reader schema allows.
"""

import importlib
import os
from collections.abc import Mapping
from typing import Any, Protocol

from gawe.core.registry import ToolRegistry
from gawe.llm.anthropic_provider import build_request_kwargs
from gawe.llm.planner import Planner
from gawe.llm.provider import LLMRequest, LLMResponse, ProviderConfigError
from gawe.llm.reader import Findings, Reader, ReaderProfile

_FILLER = (
    "The supplier shall deliver the services described in the schedule and the customer shall "
    "pay the fees within thirty days of each invoice under the terms agreed by both parties. "
)


class TokenCounter(Protocol):
    def count(self, request: LLMRequest) -> int: ...


KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def require_api_key(env: Mapping[str, str] | None = None) -> None:
    """Refuse before any request when no credential is set.

    Without one the SDK builds a client happily and then raises a bare TypeError on the first
    request, which escapes as a traceback instead of a clean refusal.
    """
    env = os.environ if env is None else env
    if not any(env.get(name, "").strip() for name in KEY_ENV_VARS):
        raise ProviderConfigError(
            "set ANTHROPIC_API_KEY first; the Makefile loads it from .env with "
            "`set -a; . ./.env; set +a`"
        )


class AnthropicTokenCounter:
    """Counts with `messages.count_tokens` (free; needs ANTHROPIC_API_KEY like a real call)."""

    def __init__(
        self,
        model: str,
        *,
        client: Any = None,
        api_errors: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        if client is None:
            require_api_key()
            client = importlib.import_module("anthropic").Anthropic()
        self._client = client
        self._api_errors = api_errors if api_errors is not None else _sdk_api_errors()
        self._model = model

    def count(self, request: LLMRequest) -> int:
        kwargs = build_request_kwargs(self._model, request)
        kwargs.pop("max_tokens")  # not an input; the endpoint does not take it
        extra = kwargs.pop("extra_body", None)
        if extra:
            kwargs["output_config"] = extra["output_config"]
        try:
            return int(self._client.messages.count_tokens(**kwargs).input_tokens)
        except self._api_errors as exc:
            # Counting is free, but the API still refuses an account with no credit or a bad key.
            detail = str(getattr(exc, "message", "") or exc)[:300]
            raise ProviderConfigError(
                f"the Anthropic API refused the token count ({type(exc).__name__}): {detail}"
            ) from exc


def _sdk_api_errors() -> tuple[type[BaseException], ...]:
    """The Anthropic SDK's API error, or nothing to catch if the SDK is not installed (the `llm`
    extra is optional, and tests pass in their own client)."""
    try:
        return (importlib.import_module("anthropic").APIError,)
    except ImportError:
        return ()


class _Captured(Exception):
    def __init__(self, request: LLMRequest) -> None:
        super().__init__("captured")
        self.request = request


class _Capture:
    def complete(self, request: LLMRequest) -> LLMResponse:
        raise _Captured(request)


def reader_request(profile: ReaderProfile, document: str) -> LLMRequest:
    """The request the Reader sends for `document` (its first attempt; a retry is the same)."""
    try:
        Reader(_Capture(), profile).read(document)
    except _Captured as captured:
        return captured.request
    raise AssertionError("the Reader did not call its provider")


def planner_request(registry: ToolRegistry, objective: str, findings: Findings) -> LLMRequest:
    """The Planner's first request. A retry adds a short note naming the problems; the 5% input
    margin covers it."""
    try:
        Planner(_Capture(), registry).plan(objective, findings)
    except _Captured as captured:
        return captured.request
    raise AssertionError("the Planner did not call its provider")


def largest_findings(profile: ReaderProfile) -> Findings:
    """The biggest findings object the profile's schema allows: every field filled to its limit."""
    return Findings(profile.schema_id, _largest(profile.schema))


def _largest(schema: Mapping[str, Any]) -> Any:
    if "enum" in schema:
        return max((v for v in schema["enum"]), key=lambda v: len(str(v)))
    kind = schema.get("type")
    if kind == "object":
        return {name: _largest(sub) for name, sub in schema.get("properties", {}).items()}
    if kind == "array":
        return [_largest(schema["items"]) for _ in range(int(schema.get("maxItems", 1)))]
    if kind == "string":
        length = int(schema.get("maxLength", 200))
        return (_FILLER * (length // len(_FILLER) + 1))[:length]
    if kind in ("number", "integer"):
        return 999_999
    if kind == "boolean":
        return False
    return None
