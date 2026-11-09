import json
import logging
import threading
from pathlib import Path

import pytest

from gawe.api.auth import (
    AuthError,
    Identity,
    TokenAuthenticator,
    hash_token,
    new_token,
    parse_bearer,
)
from gawe.api.background import InlineBackground, ThreadBackground

ID = Identity("t1", "u1", frozenset({"a"}))


def test_valid_token_authenticates() -> None:
    token = new_token()
    auth = TokenAuthenticator({hash_token(token): ID})
    assert auth.authenticate(token) == ID


@pytest.mark.parametrize("bad", [None, "", "short", "x" * 40])
def test_invalid_tokens_are_rejected(bad: str | None) -> None:
    auth = TokenAuthenticator({hash_token(new_token()): ID})
    with pytest.raises(AuthError):
        auth.authenticate(bad)


def test_the_hash_is_not_a_credential() -> None:
    token = new_token()
    auth = TokenAuthenticator({hash_token(token): ID})
    with pytest.raises(AuthError):
        auth.authenticate(hash_token(token))


@pytest.mark.parametrize("digest", ["abc", "G" * 64, "A" * 64])
def test_malformed_digests_are_refused_at_load(digest: str) -> None:
    with pytest.raises(ValueError):
        TokenAuthenticator({digest: ID})


def test_loads_from_file(tmp_path: Path) -> None:
    token = new_token()
    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            {
                "tokens": [
                    {
                        "sha256": hash_token(token),
                        "tenant_id": "t1",
                        "user_id": "u1",
                        "roles": ["a", "b"],
                    }
                ]
            }
        )
    )
    identity = TokenAuthenticator.from_file(path).authenticate(token)
    assert identity == Identity("t1", "u1", frozenset({"a", "b"}))


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer   abc ", "abc"),
        ("Basic abc", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_bearer(header: str | None, expected: str | None) -> None:
    assert parse_bearer(header) == expected


def test_new_tokens_are_long_and_unique() -> None:
    tokens = {new_token() for _ in range(50)}
    assert len(tokens) == 50 and all(len(t) >= 32 for t in tokens)


def test_inline_background_runs_immediately() -> None:
    seen: list[int] = []
    InlineBackground().submit(lambda: seen.append(1))
    assert seen == [1]


def test_thread_background_runs_jobs_and_survives_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bg = ThreadBackground(max_workers=1)
    done = threading.Event()

    def boom() -> None:
        raise RuntimeError("x")

    with caplog.at_level(logging.ERROR, logger="gawe.api"):
        bg.submit(boom)
        bg.submit(done.set)
        bg.shutdown()
    assert done.is_set()
    assert any("background job failed" in r.message for r in caplog.records)
