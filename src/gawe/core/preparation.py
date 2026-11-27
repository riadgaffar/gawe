"""Trusted, version-specific argument preparation, before policy signing."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from gawe.core.runs import RunRecord

ActionVariables = Callable[[RunRecord, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class PreparedAction:
    args: Mapping[str, Any]
    metadata: Mapping[str, Any]


class PreparationError(Exception):
    """Safe failure code; no policy authorization or dispatch occurred."""


class RecipientChanged(Exception):
    """The guarded connector proves no acceptance for this operation; refresh is safe."""

    code = "recipient_changed"


class Preparer(Protocol):
    def __call__(
        self, args: Mapping[str, Any], *, reapproval: bool, run: RunRecord | None = None
    ) -> PreparedAction:
        """`run` is the step's run; the orchestrator always passes it. A preparer that needs it
        (to read the run's ticket, say) must fail closed without it."""
        ...
