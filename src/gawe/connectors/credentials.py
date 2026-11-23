"""Credentials scoped to what a tool does (M23).

A tool's code never holds a whole system. It holds a client bound to its own credential, and
every operation goes through the system's own check first: an operation outside the credential's
scopes is refused (`credential_scope`) before the system reads or writes anything. The client
exposes all of the system's operations on purpose, so a read tool whose code tried to write would
reach the system and be refused there, not merely find the method missing.

Scopes name one operation class on one system: `<thing>:read` for a read, anything else for a
change (`assessment:write`, `envelope:send`). A read tool's credential holds only `:read` scopes.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from gawe.connectors.base import ConnectorError


class CredentialScopeError(ConnectorError):
    """The caller's credential doesn't cover the operation; nothing was done."""

    def __init__(self, principal: str, scope: str) -> None:
        super().__init__("credential_scope")
        self.principal, self.scope = principal, scope


@dataclass(frozen=True)
class Credential:
    principal: str
    scopes: frozenset[str]


def is_read_scope(scope: str) -> bool:
    return scope.endswith(":read")


class ScopedSystem(Protocol):
    # Operation (method name) -> the scope it needs. Declared by the system.
    OPERATIONS: ClassVar[Mapping[str, str]]

    def authorize(self, credential: Credential, operation: str) -> None: ...


def authorize(system: ScopedSystem, credential: Credential, operation: str) -> None:
    """The system's check: refuses an unknown operation or one outside the credential."""
    scope = system.OPERATIONS.get(operation)
    if scope is None or scope not in credential.scopes:
        raise CredentialScopeError(credential.principal, scope or operation)


class ScopedClient:
    """What a tool's code is given: the system's operations, each checked by the system first."""

    def __init__(self, system: ScopedSystem, credential: Credential) -> None:
        self._system, self._credential = system, credential

    @property
    def credential(self) -> Credential:
        return self._credential

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_") or name not in self._system.OPERATIONS:
            raise AttributeError(name)  # not an operation of this system
        method = getattr(self._system, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self._system.authorize(self._credential, name)
            return method(*args, **kwargs)

        return call
