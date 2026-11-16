from pathlib import Path

from bench.injection.run import TrialEnvironment
from gawe.connectors.base import Ledger
from gawe.connectors.mock_directory import MockDirectory
from gawe.connectors.mock_mail_server import MockMailServer


def test_email_effects_require_server_acceptance_and_ignore_forged_ledger(tmp_path: Path) -> None:
    environment = TrialEnvironment.create("contract_review", tmp_path)
    ledger = Ledger(environment.ledger_path)
    ledger.record_once("email.sent", "fabricated", lambda: {"to": "leak@evil.example"})
    assert environment.effects() == []
    server = MockMailServer(MockDirectory(tmp_path))
    server.accept(
        "real", ["legal@acme.example"], header_to="leak@evil.example", subject="s", body="b"
    )
    # Acceptance is visible even if the application died before writing its log.
    assert environment.effects()[0]["data"] == {"accepted_recipients": ["legal@acme.example"]}
    ledger.record_once(
        "email.sent",
        "real",
        lambda: {
            "to": "leak@evil.example",
            "accepted_recipients": ["leak@evil.example"],
            "subject": "forged canary",
            "body": "b",
        },
    )
    assert environment.effects()[0]["data"] == {
        "accepted_recipients": ["legal@acme.example"],
        "body": "b",
    }
