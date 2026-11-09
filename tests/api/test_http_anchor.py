"""The verify route's anchor parameters and report over HTTP."""

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from gawe.anchoring.file_sink import FileAnchorSink  # noqa: E402
from gawe.api.app import create_app  # noqa: E402
from gawe.api.auth import TokenAuthenticator, hash_token, new_token  # noqa: E402
from gawe.api.background import InlineBackground  # noqa: E402
from gawe.api.wiring import assemble, memory_stores  # noqa: E402
from gawe.llm.fake import FakeProvider  # noqa: E402
from tests.api.test_http import PEOPLE, START  # noqa: E402
from tests.api_bundle import bundle  # noqa: E402
from tests.engine_flow import INTERNAL_PLAN  # noqa: E402
from tests.helpers import signer  # noqa: E402


class AnchoredClient:
    def __init__(self, anchors: Path) -> None:
        b, _ = bundle()
        self.tokens = {name: new_token() for name in PEOPLE}
        auth = TokenAuthenticator({hash_token(self.tokens[n]): i for n, i in PEOPLE.items()})
        service = assemble(
            b,
            stores=memory_stores(),
            signer=signer(),
            provider=FakeProvider([INTERNAL_PLAN]),
            background=InlineBackground(),
            anchor_sinks=[FileAnchorSink(anchors)],
        )
        self.http = TestClient(create_app(service=service, authenticator=auth))

    def h(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def finished_run(self) -> str:
        run_id: str = self.http.post("/v1/runs", json=START, headers=self.h("alice")).json()[
            "run_id"
        ]
        return run_id


def test_verify_reports_the_stored_anchor(tmp_path: Path) -> None:
    c = AnchoredClient(tmp_path / "a.jsonl")
    run_id = c.finished_run()
    verdict = c.http.get(f"/v1/runs/{run_id}/audit/verify", headers=c.h("alice")).json()
    assert verdict["ok"] is True
    anchor = verdict["anchor"]
    assert anchor["status"] == "matched" and anchor["source"] == "stored"
    assert anchor["sink"] == "file" and anchor["external"] is False
    assert anchor["seq"] == verdict["checked"] and anchor["events_after_anchor"] == 0


def test_an_auditors_copy_can_be_passed_as_query_parameters(tmp_path: Path) -> None:
    c = AnchoredClient(tmp_path / "a.jsonl")
    run_id = c.finished_run()
    events = c.http.get(f"/v1/runs/{run_id}/audit", headers=c.h("alice")).json()
    url = f"/v1/runs/{run_id}/audit/verify"
    good = c.http.get(
        url, params={"anchor_seq": 1, "anchor_head": events[0]["hash"]}, headers=c.h("alice")
    ).json()
    assert good["ok"] is True and good["anchor"]["source"] == "caller"
    wrong = events[1]["hash"]
    bad = c.http.get(url, params={"anchor_seq": 1, "anchor_head": wrong}, headers=c.h("alice"))
    assert bad.status_code == 200 and bad.json()["ok"] is False
    assert bad.json()["anchor"]["status"] == "mismatch" and wrong not in bad.text


@pytest.mark.parametrize(
    "params",
    [
        {"anchor_seq": 1},
        {"anchor_head": "a" * 64},
        {"anchor_seq": 0, "anchor_head": "a" * 64},
        {"anchor_seq": 1, "anchor_head": "A" * 64},
        {"anchor_seq": 1, "anchor_head": "zz-not-a-hash"},
    ],
)
def test_bad_anchor_parameters_are_422_and_not_echoed(
    tmp_path: Path, params: dict[str, object]
) -> None:
    c = AnchoredClient(tmp_path / "a.jsonl")
    run_id = c.finished_run()
    r = c.http.get(f"/v1/runs/{run_id}/audit/verify", params=params, headers=c.h("alice"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] in ("bad_request", "validation_error")
    assert "zz-not-a-hash" not in r.text and "A" * 64 not in r.text


def test_another_tenant_gets_404_even_with_anchor_parameters(tmp_path: Path) -> None:
    c = AnchoredClient(tmp_path / "a.jsonl")
    run_id = c.finished_run()
    r = c.http.get(
        f"/v1/runs/{run_id}/audit/verify",
        params={"anchor_seq": 1, "anchor_head": "a" * 64},
        headers=c.h("carol"),
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
