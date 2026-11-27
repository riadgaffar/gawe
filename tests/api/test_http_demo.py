"""M29 S2: a run's model mode over HTTP: taken only under the scripted demo model."""

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from gawe.api.app import create_app  # noqa: E402
from gawe.api.auth import Identity, TokenAuthenticator, hash_token, new_token  # noqa: E402
from gawe.api.background import InlineBackground  # noqa: E402
from gawe.api.wiring import assemble, memory_stores  # noqa: E402
from gawe.apps import contract_review  # noqa: E402
from gawe.demo.model import DemoModel  # noqa: E402
from gawe.demo.scenes import BY_ID  # noqa: E402
from gawe.llm.fake import FakeProvider  # noqa: E402
from tests.helpers import signer  # noqa: E402

ALICE = Identity("t1", "alice", frozenset({"case_manager"}))
SCENE = BY_ID["c1003-review"]
BODY = {"objective": SCENE.objective, "profile": "contract_review", "document": SCENE.text()}


def client(tmp_path: Path, provider: Any) -> tuple[TestClient, dict[str, str]]:
    service = assemble(
        contract_review.bundle(tmp_path),
        stores=memory_stores(),
        signer=signer(),
        provider=provider,
        background=InlineBackground(),
    )
    token = new_token()
    auth = TokenAuthenticator({hash_token(token): ALICE})
    return TestClient(create_app(service=service, authenticator=auth)), {
        "Authorization": f"Bearer {token}"
    }


def test_the_demo_model_takes_a_mode_and_the_run_shows_it(tmp_path: Path) -> None:
    http, h = client(tmp_path, DemoModel())
    started = http.post("/v1/runs", json={**BODY, "model_mode": "fooled"}, headers=h)
    assert started.status_code == 201 and started.json()["model_mode"] == "fooled"
    run = http.get(f"/v1/runs/{started.json()['run_id']}", headers=h).json()
    assert run["model_mode"] == "fooled" and run["status"] == "paused_for_approval"


def test_a_mode_is_refused_under_another_model_and_a_bad_mode_everywhere(tmp_path: Path) -> None:
    http, h = client(tmp_path, FakeProvider([]))
    refused = http.post("/v1/runs", json={**BODY, "model_mode": "honest"}, headers=h)
    assert refused.status_code == 422 and "scripted demo model" in refused.text
    demo, hd = client(tmp_path / "demo", DemoModel())
    bad = demo.post("/v1/runs", json={**BODY, "model_mode": "sideways"}, headers=hd)
    assert bad.status_code == 422
    assert http.get("/v1/runs", headers=h).json() == []


def test_the_demo_route_is_not_found_outside_demo_mode(tmp_path: Path) -> None:
    http, _ = client(tmp_path, FakeProvider([]))
    assert http.get("/v1/demo").status_code == 404


def test_the_demo_route_lists_people_and_scenes_and_no_tokens(tmp_path: Path) -> None:
    from gawe.demo.info import demo_info

    service = assemble(
        contract_review.bundle(tmp_path),
        stores=memory_stores(),
        signer=signer(),
        provider=DemoModel(),
        background=InlineBackground(),
    )
    http = TestClient(
        create_app(service=service, authenticator=TokenAuthenticator({}), demo=demo_info("run-x"))
    )
    body = http.get("/v1/demo")  # no sign-in needed: the sign-in page lists people from it
    assert body.status_code == 200
    data = body.json()
    assert [c["key"] for c in data["companies"]] == ["careful", "big-deals", "rivals"]
    assert {s["scene_id"] for s in data["scenes"]} >= {"c1001-review", "c1003-email-counsel"}
    assert data["fixture"] == {"company": "careful", "run_id": "run-x"}
    assert "token" not in body.text and "sha256" not in body.text
