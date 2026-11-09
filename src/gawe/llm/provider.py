"""The only surface the rest of GAWE uses to talk to a language model.

A request carries a system prompt, one user message and the JSON Schema the answer must follow.
There is deliberately no `tools` field: the model can only produce data. Whether that data is
trusted is decided by the callers (Reader and Planner), never by the provider.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from gawe.core.canonical import canonical_json, sha256_hex


class LLMError(Exception):
    """Base class for language-model failures."""


class LLMUnavailableError(LLMError):
    """A transient failure (network, rate limit, server error). Worth retrying later."""


class LLMOutputError(LLMError):
    """The model answered, but not in the required form."""


class ProviderConfigError(LLMError):
    """The provider is missing required configuration (for example the model name)."""


@dataclass(frozen=True)
class LLMRequest:
    purpose: str  # "read" or "plan"; used for audit and metrics labels
    system: str
    user: str
    output_schema: Mapping[str, Any]
    max_output_tokens: int = 2048


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMProvider(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...


def request_hash(request: LLMRequest) -> str:
    """Digest of everything sent to the model, for audit trails that must not store prompts."""
    return sha256_hex(
        canonical_json(
            {
                "purpose": request.purpose,
                "system": request.system,
                "user": request.user,
                "schema": request.output_schema,
                "max_output_tokens": request.max_output_tokens,
            }
        )
    )


def response_hash(response: LLMResponse) -> str:
    return sha256_hex(response.text.encode("utf-8"))
