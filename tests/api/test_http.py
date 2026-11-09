"""HTTP-level tests. They need FastAPI and httpx (`pip install -e ".[api]"`) and are skipped
without them; `make check` on a development machine runs them."""

import json
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from gawe.api.app import create_app  # noqa: E402
from gawe.api.auth import Identity, TokenAuthenticator, hash_token, new_token  # noqa: E402
from gawe.api.background import InlineBackground  # noqa: E402
from gawe.api.wiring import assemble, memory_stores  # noqa: E402
from gawe.llm.fake import FakeProvider  # noqa: E402
from tests.api_bundle import bundle  # noqa: E402
from tests.engine_flow import EMAIL_PLAN, INTERNAL_PLAN  # noqa: E402
from tests.helpers import signer  # noqa: E402

PEOPLE = {
    "alice": Identity("t1", "alice", frozenset({"case_manager"})),
    "bob": Identity("t1", "bob", frozenset({"legal_reviewer"})),
    "carol": Identity("t2", "carol", frozenset({"case_manager", "legal_reviewer"})),
}


class Client:
    def __init__(self, plan: dict[str, Any], **app_kw: Any) -> None:
        b, self.connector = bundle()
        self.tokens = {name: new_token() for name in PEOPLE}
        auth = TokenAuthenticator({hash_token(self.tokens[n]): i for n, i in PEOPLE.items()})
        service = assemble(
            b,
            stores=memory_stores(),
            signer=signer(),
            provider=FakeProvider([plan]),
            background=InlineBackground(),
        )
        self.http = TestClient(
            create_app(service=service, authenticator=auth, poll_interval=0.01, **app_kw)
        )

    def h(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


START = {"objective": "Review and email counsel", "profile": "contract_review"}


def test_health_needs_no_auth_and_everything_else_does() -> None:
    c = Client(INTERNAL_PLAN)
    assert c.http.get("/healthz").json() == {"status": "ok"}
    for path in ("/v1/me", "/v1/tools", "/v1/runs", "/v1/approvals"):
        r = c.http.get(path)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert r.json()["error"]["code"] == "unauthorized"
    assert (
        c.http.get("/v1/runs", headers={"Authorization": "Bearer " + "x" * 40}).status_code == 401
    )
    assert c.http.get("/v1/runs", headers={"Authorization": "Basic abc"}).status_code == 401


def test_me_returns_only_the_callers_identity() -> None:
    c = Client(INTERNAL_PLAN)
    assert c.http.get("/v1/me", headers=c.h("bob")).json() == {
        "user_id": "bob",
        "tenant_id": "t1",
        "roles": ["legal_reviewer"],
    }
    roles = c.http.get("/v1/me", headers=c.h("carol")).json()["roles"]
    assert roles == ["case_manager", "legal_reviewer"]  # sorted, stable


def test_repeated_bad_tokens_get_429_with_retry_after_and_good_users_elsewhere_are_fine() -> None:
    from gawe.api.throttle import FailureThrottle

    c = Client(INTERNAL_PLAN, throttle=FailureThrottle(limit=3, window_seconds=60))
    bad = {"Authorization": "Bearer " + "x" * 40}
    assert [c.http.get("/v1/runs", headers=bad).status_code for _ in range(3)] == [401] * 3
    blocked = c.http.get("/v1/runs", headers=bad)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert blocked.json()["error"]["code"] == "too_many_requests"
    # Even a valid token from the blocked address waits: the address, not the token, is limited.
    assert c.http.get("/v1/runs", headers=c.h("alice")).status_code == 429


def test_valid_requests_are_never_counted_against_the_limit() -> None:
    from gawe.api.throttle import FailureThrottle

    c = Client(INTERNAL_PLAN, throttle=FailureThrottle(limit=2, window_seconds=60))
    assert all(c.http.get("/v1/runs", headers=c.h("alice")).status_code == 200 for _ in range(10))


def test_security_headers() -> None:
    r = Client(INTERNAL_PLAN).http.get("/healthz")
    assert (
        r.headers["x-content-type-options"] == "nosniff"
        and r.headers["cache-control"] == "no-store"
    )


def test_start_and_read_a_run() -> None:
    c = Client(INTERNAL_PLAN)
    r = c.http.post("/v1/runs", json=START, headers=c.h("alice"))
    assert r.status_code == 201
    run_id = r.json()["run_id"]
    got = c.http.get(f"/v1/runs/{run_id}", headers=c.h("alice")).json()
    assert got["status"] == "completed" and len(got["steps"]) == 2
    assert [x["run_id"] for x in c.http.get("/v1/runs", headers=c.h("alice")).json()] == [run_id]
    assert c.http.get("/v1/tools", headers=c.h("alice")).json()


def test_validation_errors_do_not_echo_input() -> None:
    c = Client(INTERNAL_PLAN)
    secret = "TOP-SECRET-CONTRACT-TEXT"
    r = c.http.post(
        "/v1/runs", json={**START, "document": secret, "bogus": secret}, headers=c.h("alice")
    )
    assert r.status_code == 422
    assert secret not in r.text
    assert r.json()["error"]["code"] == "validation_error"


def test_oversized_body_is_rejected() -> None:
    c = Client(INTERNAL_PLAN, max_body_bytes=1000)
    r = c.http.post("/v1/runs", json={**START, "document": "x" * 5000}, headers=c.h("alice"))
    assert r.status_code == 413


def test_approval_flow_over_http() -> None:
    c = Client(EMAIL_PLAN)
    run_id = c.http.post("/v1/runs", json=START, headers=c.h("alice")).json()["run_id"]
    assert c.http.get("/v1/approvals", headers=c.h("alice")).json() == []
    assert c.http.get("/v1/approvals", headers=c.h("carol")).json() == []
    (item,) = c.http.get("/v1/approvals", headers=c.h("bob")).json()
    assert item["resolved_args"]["to"] == "counsel@partner.com"

    url = f"/v1/approvals/{item['approval_id']}/decision"
    bad = c.http.post(
        url, json={"decision": "approved", "action_hash": "0" * 64}, headers=c.h("bob")
    )
    assert bad.status_code == 409
    nope = c.http.post(url, json={"decision": "approved", "action_hash": "zz"}, headers=c.h("bob"))
    assert nope.status_code == 422
    denied = c.http.post(
        url, json={"decision": "approved", "action_hash": item["action_hash"]}, headers=c.h("alice")
    )
    assert denied.status_code == 403
    ok = c.http.post(
        url, json={"decision": "approved", "action_hash": item["action_hash"]}, headers=c.h("bob")
    )
    assert ok.status_code == 200 and ok.json()["decision"] == "approved"
    assert c.http.get(f"/v1/runs/{run_id}", headers=c.h("alice")).json()["status"] == "completed"


def test_other_tenant_gets_404() -> None:
    c = Client(INTERNAL_PLAN)
    run_id = c.http.post("/v1/runs", json=START, headers=c.h("alice")).json()["run_id"]
    for method, path in (
        ("get", f"/v1/runs/{run_id}"),
        ("post", f"/v1/runs/{run_id}/cancel"),
        ("get", f"/v1/runs/{run_id}/audit"),
        ("get", f"/v1/runs/{run_id}/audit/verify"),
        ("get", f"/v1/runs/{run_id}/events"),
    ):
        r = getattr(c.http, method)(path, headers=c.h("carol"))
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "not_found"


def test_cancel_resume_and_audit() -> None:
    c = Client(EMAIL_PLAN)
    run_id = c.http.post("/v1/runs", json=START, headers=c.h("alice")).json()["run_id"]
    assert c.http.post(f"/v1/runs/{run_id}/cancel", headers=c.h("bob")).status_code == 403
    assert c.http.post(f"/v1/runs/{run_id}/resume", headers=c.h("alice")).status_code == 202
    assert c.http.post(f"/v1/runs/{run_id}/cancel", headers=c.h("alice")).json()["status"] == (
        "cancelled"
    )
    events = c.http.get(f"/v1/runs/{run_id}/audit", headers=c.h("alice")).json()
    types = [e["type"] for e in events]
    assert types[0] == "run.created" and "run.cancelled" in types
    assert types[-1] == "approval.closed"  # the open approval is closed when the run is cancelled
    verdict = c.http.get(f"/v1/runs/{run_id}/audit/verify", headers=c.h("alice")).json()
    assert verdict["ok"] is True and verdict["checked"] == len(events)


def test_event_stream_replays_and_ends() -> None:
    c = Client(INTERNAL_PLAN)
    run_id = c.http.post("/v1/runs", json=START, headers=c.h("alice")).json()["run_id"]
    with c.http.stream("GET", f"/v1/runs/{run_id}/events", headers=c.h("alice")) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: audit" in body and "event: end" in body
    assert json.loads(body.rsplit("data: ", 1)[1].strip()) == {"status": "completed"}
    ids = [int(line[4:]) for line in body.splitlines() if line.startswith("id: ")]
    assert ids == sorted(ids) and ids[0] == 1

    with c.http.stream(
        "GET", f"/v1/runs/{run_id}/events", headers={**c.h("alice"), "Last-Event-ID": str(ids[-1])}
    ) as r:
        resumed = "".join(r.iter_text())
    assert "event: audit" not in resumed and "event: end" in resumed


def test_cors_is_off_by_default_and_explicit_when_set() -> None:
    c = Client(INTERNAL_PLAN)
    r = c.http.get("/healthz", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in r.headers
    c2 = Client(INTERNAL_PLAN, cors_origins=["http://localhost:3000"])
    ok = c2.http.get("/healthz", headers={"Origin": "http://localhost:3000"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    other = c2.http.get("/healthz", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in other.headers
