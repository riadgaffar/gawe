"""LLMProvider backed by the Anthropic Messages API.

Structured output uses the API's JSON-schema output feature (`output_config.format`) when the
schema allows it. Recent models (Sonnet 5.5, Opus 5.5 and others) refuse forced tool use, which an
earlier version of this file relied on. When a schema has an open object (the Planner's free-form
tool arguments) the feature cannot express it, so the model is instead told to reply with only a
JSON object matching the schema. Either way the answer is just text: the Reader and Planner parse
and validate it themselves, and the repair loop covers a malformed reply.

Configuration comes from the environment: the SDK reads ANTHROPIC_API_KEY, and the model name
must be given as `model=` or GAWE_LLM_MODEL (nothing is hardcoded). The SDK is an optional
dependency (`pip install "gawe[llm]"`) and is imported lazily.
"""

import importlib
import json
import os
import re
from typing import Any

from gawe.llm.provider import (
    LLMOutputError,
    LLMRequest,
    LLMResponse,
    LLMUnavailableError,
    ProviderConfigError,
)
from gawe.llm.schema_adapt import adapt_schema

_TRANSIENT_NAMES = (
    "APIConnectionError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
)
# Errors that mean "this setup cannot work", not "try again later".
_CONFIG_NAMES = ("AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError")
_JSON_ONLY = (
    "\n\nReply with ONLY a single JSON object that matches this JSON Schema. No prose, no "
    "markdown, no code fences.\nJSON Schema:\n{schema}"
)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_MAX_ERROR_CHARS = 300


def _load_sdk() -> Any:
    try:
        return importlib.import_module("anthropic")
    except ImportError as exc:
        raise ProviderConfigError(
            'the anthropic package is not installed; run: pip install "gawe[llm]"'
        ) from exc


def build_request_kwargs(model: str, request: LLMRequest) -> dict[str, Any]:
    """The Messages API arguments for `request`. Shared with token counting, so a count measures
    exactly what a real call would send."""
    api_schema = adapt_schema(request.output_schema)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": request.max_output_tokens,
        "system": request.system,
        "messages": [{"role": "user", "content": request.user}],
    }
    if api_schema is not None:
        # extra_body works with any SDK version, including ones older than output_config.
        kwargs["extra_body"] = {
            "output_config": {"format": {"type": "json_schema", "schema": api_schema}}
        }
    else:
        kwargs["system"] = request.system + _JSON_ONLY.format(
            schema=json.dumps(dict(request.output_schema), sort_keys=True)
        )
    return kwargs


class AnthropicProvider:
    def __init__(
        self,
        *,
        model: str | None = None,
        client: Any = None,
        timeout: float = 60.0,
        transient_errors: tuple[type[BaseException], ...] | None = None,
        config_errors: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        resolved = model or os.environ.get("GAWE_LLM_MODEL", "").strip()
        if not resolved:
            raise ProviderConfigError("set GAWE_LLM_MODEL (or pass model=) to choose the model")
        self._model = resolved
        sdk: Any = None
        if client is None:
            sdk = _load_sdk()
            client = sdk.Anthropic(timeout=timeout)  # key comes from ANTHROPIC_API_KEY
        self._client = client
        if transient_errors is None:
            transient_errors = (
                tuple(getattr(sdk, name) for name in _TRANSIENT_NAMES if hasattr(sdk, name))
                if sdk is not None
                else ()
            )
        self._transient = transient_errors
        if config_errors is None:
            config_errors = (
                tuple(getattr(sdk, name) for name in _CONFIG_NAMES if hasattr(sdk, name))
                if sdk is not None
                else ()
            )
        self._config = config_errors

    def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs = build_request_kwargs(self._model, request)
        try:
            message = self._client.messages.create(**kwargs)
        except self._transient as exc:
            raise LLMUnavailableError(type(exc).__name__) from exc
        except self._config as exc:
            detail = str(getattr(exc, "message", "") or type(exc).__name__)[:_MAX_ERROR_CHARS]
            raise ProviderConfigError(
                f"the Anthropic API rejected the request ({type(exc).__name__}): {detail}. "
                "Check ANTHROPIC_API_KEY and GAWE_LLM_MODEL."
            ) from exc

        stop = getattr(message, "stop_reason", None)
        if stop in ("refusal", "max_tokens"):
            raise LLMOutputError(f"the model stopped early ({stop})")
        text = next(
            (
                str(block.text)
                for block in getattr(message, "content", [])
                if getattr(block, "type", None) == "text"
            ),
            None,
        )
        if text is None:
            raise LLMOutputError("the model returned no text")
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1)
        usage = getattr(message, "usage", None)
        return LLMResponse(
            text=text,
            model=str(getattr(message, "model", self._model)),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
