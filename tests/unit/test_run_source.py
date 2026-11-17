"""A run bound to its source record: the server fetches the ticket, never the caller."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.helpers import signer

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.errors import BadRequestError
from gawe.api.wiring import assemble, load_app_rules, memory_stores
from gawe.apps import contract_review
from gawe.apps import it_access as ia
from gawe.connectors.mock_tickets import MockTickets, Ticket
from gawe.core.sources import SourceError
from gawe.llm.fake import FakeProvider

AGENT = Identity("t1", "ivy", frozenset({"it_agent"}))
OTHER_TENANT = Identity("t2", "olga", frozenset({"it_agent"}))
DANA = "dana.ortiz@acme.example"
MARKER = "zq-ticket-body-marker"
FINDINGS = {"requester_email": DANA, "system": "wiki", "level": "read", "justification": "Runbook"}
PLAN = {
    "steps": [
        {
            "id": "grant",
            "tool": "iam.grant_access",
            "args": {"email": DANA, "system": "wiki", "level": "read"},
            "depends_on": [],
        }
    ]
}


class Desk:
    def __init__(self, path: Path, script: list[Any] | None = None) -> None:
        self.tickets = MockTickets.in_state_dir(path)
        self.provider = FakeProvider(script if script is not None else [FINDINGS, PLAN])
        self.stores = memory_stores()
        self.service = assemble(
            ia.bundle(path, tickets=self.tickets),
            stores=self.stores,
            signer=signer(),
            provider=self.provider,
            background=InlineBackground(),
        )

    def file(self, who: Identity = AGENT, ticket_id: str = "AR-1", body: str = MARKER) -> str:
        self.tickets.create(
            who.tenant_id, ticket_id, submitted_by=DANA, channel="portal", body=body
        )
        return ticket_id

    def start(self, who: Identity = AGENT, **kw: Any) -> Any:
        return self.service.start_run(
            who, objective="Process the request", profile="it_access", **kw
        )


def test_the_server_fetches_the_ticket_and_the_reader_reads_it(tmp_path: Path) -> None:
    d = Desk(tmp_path)
    started = d.start(source_id=d.file())
    view = d.service.get_run(AGENT, started.run_id)
    assert started.source_id == view.source_id == "AR-1" and view.status == "completed"
    reader = d.provider.requests[0]
    assert reader.purpose == "read" and MARKER in reader.user
    run = d.stores.runs.get(view.run_id)
    assert run.source_id == "AR-1"
    assert run.document is None  # cleared when the run ended, as for uploads


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"document": "uploaded text"}, "reads its document from its source record"),
        ({}, "source_id is required"),
        ({"source_id": "../etc/passwd"}, "malformed"),
        ({"source_id": "AR-404"}, "source record unavailable: source_not_found"),
    ],
)
def test_a_sourced_run_takes_only_a_known_ticket_id(
    tmp_path: Path, kwargs: dict[str, Any], message: str
) -> None:
    d = Desk(tmp_path, script=[])
    d.file()
    with pytest.raises(BadRequestError, match=message):
        d.start(**kwargs)
    assert d.provider.requests == [] and d.stores.runs.list_for_tenant("t1") == []


def test_another_tenants_ticket_is_not_found(tmp_path: Path) -> None:
    d = Desk(tmp_path, script=[])
    d.file(OTHER_TENANT)
    with pytest.raises(BadRequestError, match="source_not_found"):
        d.start(AGENT, source_id="AR-1")


def test_an_unreadable_ticket_system_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = Desk(tmp_path, script=[])
    d.file()

    def broken(*args: Any) -> Any:
        raise OSError("ticket database unavailable")

    monkeypatch.setattr(d.tickets, "get", broken)
    with pytest.raises(BadRequestError, match="source_unreadable"):
        d.start(source_id="AR-1")


def test_a_profile_without_a_source_refuses_a_source_id(tmp_path: Path) -> None:
    service = assemble(
        contract_review.bundle(tmp_path),
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider([]),
        background=InlineBackground(),
    )
    who = Identity("t1", "alice", frozenset({"case_manager"}))
    with pytest.raises(BadRequestError, match="does not read from a source record"):
        service.start_run(who, objective="Review", profile="contract_review", source_id="AR-1")


# --- the ticket system -----------------------------------------------------------------------


def test_who_filed_a_ticket_and_how_cannot_change(tmp_path: Path) -> None:
    tickets = MockTickets.in_state_dir(tmp_path)
    tickets.create("t1", "AR-1", submitted_by=DANA, channel="portal", body="first")
    with pytest.raises(ValueError, match="already exists"):
        tickets.create("t1", "AR-1", submitted_by="mallory@acme.example", channel="email", body="x")
    closed = tickets.set_status("t1", "AR-1", "closed")
    assert (closed.submitted_by, closed.channel, closed.body) == (DANA, "portal", "first")
    again = MockTickets.in_state_dir(tmp_path).get("t1", "AR-1")  # a new process reads the file
    assert again == closed
    assert not hasattr(tickets, "set_submitter") and not hasattr(tickets, "update")


def test_unknown_channels_and_statuses_are_refused(tmp_path: Path) -> None:
    tickets = MockTickets.in_state_dir(tmp_path)
    with pytest.raises(ValueError, match="channel or status"):
        tickets.create("t1", "AR-1", submitted_by=DANA, channel="fax", body="x")  # type: ignore[arg-type]
    tickets.create("t1", "AR-2", submitted_by=DANA, channel="email", body="x")
    with pytest.raises(ValueError, match="status"):
        tickets.set_status("t1", "AR-2", "merged")  # type: ignore[arg-type]


def test_sample_tickets_are_seeded_lazily_for_the_development_tenant(tmp_path: Path) -> None:
    b = ia.bundle(tmp_path)
    assert not (tmp_path / "tickets.sqlite3").exists()  # building the app wrote nothing
    tickets = b.sources["it_access"]
    assert isinstance(tickets, MockTickets)
    samples = {t.ticket_id: t for t in ia.sample_tickets()}
    assert set(samples) == {"AR-2041", "AR-2042"}
    for ticket_id, sample in samples.items():
        stored = tickets.get(sample.tenant_id, ticket_id)
        assert stored.submitted_by in sample.body and stored.channel == "portal"
    with pytest.raises(SourceError):
        tickets.get("t1", "AR-2041")  # seeded for the development tenant only


def test_a_seed_never_overwrites_a_stored_ticket(tmp_path: Path) -> None:
    tickets = MockTickets.in_state_dir(tmp_path)
    tickets.create("t1", "AR-1", submitted_by=DANA, channel="portal", body="stored")
    tickets.seed([Ticket("t1", "AR-1", "mallory@acme.example", "email", "open", "seeded")])
    assert tickets.get("t1", "AR-1").submitted_by == DANA


# --- startup ---------------------------------------------------------------------------------


def test_sources_must_name_a_profile_with_a_reader(tmp_path: Path) -> None:
    b = ia.bundle(tmp_path)
    with pytest.raises(ValueError, match="profiles the app does not have"):
        load_app_rules(replace(b, sources={"other": b.sources["it_access"]}))
    with pytest.raises(ValueError, match="needs a Reader"):
        load_app_rules(replace(b, reader_profile=None))
    assert load_app_rules(b)
