"""Trusted, version-specific argument preparation, before policy signing."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


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
    def __call__(self, args: Mapping[str, Any], *, reapproval: bool) -> PreparedAction: ...
