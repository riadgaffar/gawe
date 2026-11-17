"""Starting an IT-access run over HTTP: a ticket id, never an upload."""

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from gawe.api.app import create_app  # noqa: E402
from gawe.api.auth import Identity, TokenAuthenticator, hash_token, new_token  # noqa: E402
from gawe.api.background import InlineBackground  # noqa: E402
from gawe.api.wiring import assemble, memory_stores  # noqa: E402
from gawe.apps import it_access  # noqa: E402
from gawe.connectors.mock_tickets import MockTickets  # noqa: E402
from gawe.llm.fake import FakeProvider  # noqa: E402
from tests.helpers import signer  # noqa: E402

IVY = Identity("t1", "ivy", frozenset({"it_agent"}))
BODY = {"objective": "Process the request", "profile": "it_access"}


def client(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    tickets = MockTickets.in_state_dir(tmp_path)
    tickets.create(
        "t1", "AR-7", submitted_by="dana.ortiz@acme.example", channel="portal", body="ticket"
    )
    service = assemble(
        it_access.bundle(tmp_path, tickets=tickets),
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider([RuntimeError("no model in this test")]),
        background=InlineBackground(),
    )
    token = new_token()
    auth = TokenAuthenticator({hash_token(token): IVY})
    return TestClient(create_app(service=service, authenticator=auth)), {
        "Authorization": f"Bearer {token}"
    }


def test_a_run_starts_from_a_ticket_id(tmp_path: Path) -> None:
    http, h = client(tmp_path)
    started = http.post("/v1/runs", json={**BODY, "source_id": "AR-7"}, headers=h)
    assert started.status_code == 201 and started.json()["source_id"] == "AR-7"
    run = http.get(f"/v1/runs/{started.json()['run_id']}", headers=h).json()
    assert run["source_id"] == "AR-7"


def test_an_upload_or_an_unknown_ticket_is_refused(tmp_path: Path) -> None:
    http, h = client(tmp_path)
    upload = http.post("/v1/runs", json={**BODY, "document": "pasted ticket"}, headers=h)
    assert upload.status_code == 422 and "source record" in upload.text
    unknown = http.post("/v1/runs", json={**BODY, "source_id": "AR-404"}, headers=h)
    assert unknown.status_code == 422 and "source_not_found" in unknown.text
    too_long = http.post("/v1/runs", json={**BODY, "source_id": "A" * 65}, headers=h)
    assert too_long.status_code == 422
    assert http.get("/v1/runs", headers=h).json() == []
