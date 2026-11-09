"""Bearer-token authentication stub.

Tokens are random secrets given to people out of band. The server keeps only their SHA-256 hashes
(in a JSON file named by `GAWE_API_TOKENS_FILE`), so a leaked config file does not leak working
credentials. Each hash maps to a fixed identity: tenant, user and roles.

This is a stub for the MVP. Production replaces it with the organization's identity provider
(OIDC) and reads roles from the database; `Identity` is the only thing the rest of the API sees.
"""

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

MIN_TOKEN_CHARS = 32


class AuthError(Exception):
    """The request carries no valid credentials."""


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    user_id: str
    roles: frozenset[str]


def hash_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def parse_bearer(header: str | None) -> str | None:
    """The token from an `Authorization: Bearer <token>` header, or None."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        return None
    return value


class TokenAuthenticator:
    def __init__(self, identities_by_token_hash: Mapping[str, Identity]) -> None:
        for digest in identities_by_token_hash:
            if len(digest) != 64 or digest != digest.lower():
                raise ValueError("token entries must be lowercase SHA-256 hex digests")
        self._identities = dict(identities_by_token_hash)

    @classmethod
    def from_file(cls, path: Path) -> "TokenAuthenticator":
        data = json.loads(path.read_text(encoding="utf-8"))
        entries: dict[str, Identity] = {}
        for item in data["tokens"]:
            entries[item["sha256"]] = Identity(
                tenant_id=item["tenant_id"],
                user_id=item["user_id"],
                roles=frozenset(item.get("roles", [])),
            )
        return cls(entries)

    def authenticate(self, token: str | None) -> Identity:
        # Looking up the hash of the presented token reveals nothing about any stored secret.
        if not token or len(token) < MIN_TOKEN_CHARS:
            raise AuthError("invalid credentials")
        identity = self._identities.get(hash_token(token))
        if identity is None:
            raise AuthError("invalid credentials")
        return identity
