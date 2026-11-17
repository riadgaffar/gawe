"""Where a run's document comes from, when its profile names a source.

A profile with a source (IT access reads tickets) takes a `source_id` from the caller instead of
an uploaded document. The server fetches the text itself, so the text the Reader reads and the
record policy trusts are the same record. Fetching fails closed with a code, before any planning.
"""

from typing import Protocol


class SourceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DocumentSource(Protocol):
    def fetch(self, tenant_id: str, source_id: str) -> str:
        """The record's document text, or SourceError (`source_not_found`, ...)."""
        ...
