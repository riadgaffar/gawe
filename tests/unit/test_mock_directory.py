"""Directory resolution and independent mock mail-server state, including process boundaries."""

import multiprocessing
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gawe.connectors.base import ConnectorError, Ledger
from gawe.connectors.mock_directory import DirectoryEntry, Expansion, MockDirectory
from gawe.connectors.mock_mail_server import MockMailServer

SEED = Path(__file__).parents[2] / "src/gawe/connectors/seeds/directory.json"
MAILBOX = DirectoryEntry(kind="mailbox")


def seeded(path):
    return MockDirectory.from_seed(path, SEED)


def send(server, expansion, key="operation", **kwargs):
    return server.submit(
        key, expansion, header_to=expansion.requested_to, subject="Review", body="Text", **kwargs
    )


@pytest.mark.parametrize(
    "target, expected",
    [
        ("legal@ACME.EXAMPLE", ("legal@acme.example",)),
        ("nested@acme.example", ("legal@acme.example", "ops@acme.example")),
        ("reviewers@acme.example", ("legal@acme.example", "ops@acme.example")),
        ("mixed@acme.example", ("legal@acme.example", "vendor@outside.example")),
        ("blocked-members@acme.example", ("attacker@evil.example", "legal@acme.example")),
        ("list@outside.example", ("list@outside.example",)),
        ("alias@outside.example", ("legal@acme.example",)),
        ("new@outside.example", ("new@outside.example",)),
    ],
)
def test_expansion_controls(tmp_path, target, expected):
    assert seeded(tmp_path).expand(target).mailboxes == expected


@pytest.mark.parametrize(
    "target, code",
    [
        ("private@acme.example", "directory_unreadable"),
        ("missing@acme.example", "directory_unknown_internal"),
        ("legal@acme.example\n", "directory_invalid_address"),
        ("legal@acme.example,ops@acme.example", "directory_invalid_address"),
        ("Name <legal@acme.example>", "directory_invalid_address"),
        ("x@acme.example.", "directory_invalid_address"),
    ],
)
def test_unreadable_targets_never_fall_back(tmp_path, target, code):
    with pytest.raises(ConnectorError, match=code):
        seeded(tmp_path).expand(target)


def test_local_case_plus_and_idna_identity(tmp_path):
    directory = MockDirectory(tmp_path, entries={"Üser+Tag@bücher.example": MAILBOX})
    assert directory.expand("Üser+Tag@xn--bcher-kva.example").mailboxes == (
        "Üser+Tag@xn--bcher-kva.example",
    )
    directory.put("Local+Tag@acme.example", MAILBOX)
    assert directory.expand("Local+Tag@ACME.EXAMPLE").mailboxes == ("Local+Tag@acme.example",)
    with pytest.raises(ConnectorError, match="directory_unknown_internal"):
        directory.expand("local+Tag@acme.example")


def test_cycles_empty_groups_and_nested_unreadable_members(tmp_path):
    directory = seeded(tmp_path)
    for name, members, code in [
        ("empty", (), "directory_empty_group"),
        ("cycle", ("cycle@acme.example",), "directory_cycle"),
        ("nested_private", ("legal@acme.example", "private@acme.example"), "directory_unreadable"),
        ("missing", ("absent@acme.example",), "directory_unknown_internal"),
        ("malformed", ("x@acme..example",), "directory_invalid_address"),
    ]:
        address = name + "@acme.example"
        directory.put(address, DirectoryEntry(kind="group", members=members))
        with pytest.raises(ConnectorError, match=code):
            directory.expand(address)
    directory.put("a@acme.example", DirectoryEntry(kind="alias", members=("b@acme.example",)))
    directory.put("b@acme.example", DirectoryEntry(kind="group", members=("a@acme.example",)))
    with pytest.raises(ConnectorError, match="directory_cycle"):
        directory.expand("a@acme.example")


def test_expansion_size_and_depth_bounds(tmp_path):
    directory = seeded(tmp_path)
    members = tuple(f"p{i}@outside.example" for i in range(100))
    directory.put("large@acme.example", DirectoryEntry(kind="group", members=members))
    assert len(directory.expand("large@acme.example").mailboxes) == 100
    directory.put(
        "large@acme.example",
        DirectoryEntry(kind="group", members=members + ("overflow@outside.example",)),
    )
    with pytest.raises(ConnectorError, match="directory_size_exceeded"):
        directory.expand("large@acme.example")
    target = "legal@acme.example"
    for i in range(17):
        alias = f"depth{i}@acme.example"
        directory.put(alias, DirectoryEntry(kind="alias", members=(target,)))
        target = alias
        if i == 15:
            assert directory.expand(target).mailboxes == ("legal@acme.example",)
    with pytest.raises(ConnectorError, match="directory_depth_exceeded"):
        directory.expand(target)


def test_revision_persists_and_lookup_time_is_not_identity(tmp_path):
    directory = seeded(tmp_path)
    before = directory.expand("legal@acme.example")
    directory.put("unrelated@acme.example", MAILBOX)
    reconstructed = seeded(tmp_path)
    after = reconstructed.expand("legal@acme.example")
    assert before.mailboxes == after.mailboxes and before.directory_marker != after.directory_marker
    later = after.model_copy(update={"lookup_at": after.lookup_at + timedelta(days=1)})
    server = MockMailServer(reconstructed)
    assert send(server, after) == send(server, later)
    with pytest.raises(ValueError, match="domain configuration differs"):
        MockDirectory(tmp_path, internal_domains=("other.example",))


@pytest.mark.parametrize(
    "target", ["all-staff@acme.example", "reviewers@acme.example", "private@acme.example"]
)
def test_server_rejects_group_or_alias_as_envelope(tmp_path, target):
    server = MockMailServer(seeded(tmp_path))
    with pytest.raises(ConnectorError):
        server.accept("operation", [target], header_to="legal@acme.example", subject="x", body="x")
    assert server.read("operation") is None


def test_acceptance_is_independent_of_header_and_app_ledger(tmp_path):
    directory = seeded(tmp_path)
    server = MockMailServer(directory)
    snapshot = directory.expand("all-staff@acme.example")
    record = send(server, snapshot)
    assert record.accepted_recipients == ("legal@acme.example", "ops@acme.example")
    assert record.header_to == "all-staff@acme.example"
    Ledger(tmp_path / "ledger.jsonl").record_once(
        "email.sent", "operation", lambda: {"to": "attacker@evil.example"}
    )
    assert MockMailServer(seeded(tmp_path)).read("operation") == record
    assert "Text" not in record.model_dump_json()
    with pytest.raises(ConnectorError, match="mail_idempotency_conflict"):
        server.submit("operation", snapshot, header_to="changed", subject="Review", body="Text")


@pytest.mark.parametrize("change", ["added", "removed", "marker", "unreadable"])
def test_mutation_at_submission_boundary_blocks_without_acceptance(tmp_path, change):
    directory = seeded(tmp_path)
    server = MockMailServer(directory)
    snapshot = directory.expand("all-staff@acme.example")

    def mutate():
        if change == "marker":
            directory.put("unrelated@acme.example", MAILBOX)
        elif change == "unreadable":
            directory.put("all-staff@acme.example", DirectoryEntry(kind="unreadable_group"))
        else:
            members = (
                ("legal@acme.example",)
                if change == "removed"
                else (*snapshot.mailboxes, "external@outside.example")
            )
            directory.put("all-staff@acme.example", DirectoryEntry(kind="group", members=members))

    with pytest.raises(ConnectorError, match="directory_(changed|unreadable)"):
        send(server, snapshot, before_check=mutate)
    assert server.read("operation") is None


def test_directory_cannot_change_between_check_and_acceptance(tmp_path):
    directory = seeded(tmp_path)
    snapshot = directory.expand("legal@acme.example")
    attempted = []

    def mutate_after_check():
        with (
            sqlite3.connect(directory.state.path, timeout=0) as competing,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            competing.execute("UPDATE metadata SET value='changed' WHERE name='revision'")
        attempted.append(True)

    assert (
        send(
            MockMailServer(directory), snapshot, after_check=mutate_after_check
        ).accepted_recipients
        == snapshot.mailboxes
    )
    assert attempted == [True]
    directory.put("legal@acme.example", MAILBOX)  # the guard is released after acceptance
    assert directory.expand("legal@acme.example").directory_marker != snapshot.directory_marker
    assert send(MockMailServer(directory), snapshot).accepted_recipients == snapshot.mailboxes


def _submit_process(path, snapshot, start, results):
    start.wait(10)
    record = send(
        MockMailServer(MockDirectory(Path(path))), Expansion.model_validate_json(snapshot)
    )
    results.put(record.model_dump_json())


def test_separate_processes_accept_one_operation(tmp_path):
    directory = seeded(tmp_path)
    snapshot = directory.expand("all-staff@acme.example")
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    workers = [
        ctx.Process(
            target=_submit_process, args=(str(tmp_path), snapshot.model_dump_json(), start, results)
        )
        for _ in range(4)
    ]
    try:
        for worker in workers:
            worker.start()
        start.set()
        records = [results.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
        assert len(set(records)) == 1
        with directory.state.transaction() as conn:
            assert conn.execute("SELECT count(*) FROM accepted").fetchone()[0] == 1
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        results.close()


def _crash_before_commit(path, snapshot):
    class CrashServer(MockMailServer):
        def _accept(self, *args, **kwargs):
            super()._accept(*args, **kwargs)
            os._exit(9)  # acceptance and idempotency insert have not committed

    send(CrashServer(MockDirectory(Path(path))), Expansion.model_validate_json(snapshot))


def test_process_crash_rolls_back_acceptance_and_retry_is_safe(tmp_path):
    directory = seeded(tmp_path)
    snapshot = directory.expand("legal@acme.example")
    worker = multiprocessing.get_context("spawn").Process(
        target=_crash_before_commit, args=(str(tmp_path), snapshot.model_dump_json())
    )
    worker.start()
    worker.join(20)
    try:
        assert worker.exitcode == 9
        server = MockMailServer(MockDirectory(tmp_path))
        assert server.read("operation") is None
        assert send(server, snapshot) == send(MockMailServer(MockDirectory(tmp_path)), snapshot)
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)


def test_snapshot_timestamps_are_injected_and_frozen(tmp_path):
    at = datetime(2026, 11, 12, tzinfo=UTC)
    directory = MockDirectory(tmp_path, entries={"x@acme.example": MAILBOX}, clock=lambda: at)
    assert directory.expand("x@acme.example").lookup_at == at
