"""Trusted email v2 preparation; no model-supplied delivery metadata is accepted."""

from collections.abc import Mapping
from typing import Any

from gawe.connectors.base import ConnectorError
from gawe.connectors.mock_directory import MockDirectory
from gawe.core.preparation import PreparationError, PreparedAction


class EmailPreparer:
    def __init__(self, directory: MockDirectory) -> None:
        self.directory = directory

    def __call__(self, args: Mapping[str, Any], *, reapproval: bool) -> PreparedAction:
        try:
            expanded = self.directory.expand(args["to"])
        except ConnectorError as exc:
            raise PreparationError(exc.code) from exc
        return PreparedAction(
            args={
                **args,
                "delivery": {
                    "mailboxes": list(expanded.mailboxes),
                    "directory_marker": expanded.directory_marker,
                    "reapproval_required": reapproval,
                },
            },
            metadata={
                "recipient_lookup_at": expanded.lookup_at.isoformat(),
                "reapproval_required": reapproval,
            },
        )
