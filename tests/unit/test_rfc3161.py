"""Signed-timestamp anchors, checked offline against replies recorded from the real authorities.

The fixtures in tests/fixtures/tsa/ were recorded once with scripts/record_tsa_fixture.py. They
cover only the digest of a made-up record. Certificates are checked as of the stamped time, so
these tests keep passing after the certificates expire.
"""

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("asn1crypto")

from asn1crypto import cms, core, tsp  # noqa: E402

from gawe.anchoring import rfc3161  # noqa: E402
from gawe.anchoring.config import AnchorConfigError, sinks_from_env  # noqa: E402
from gawe.anchoring.file_sink import FileAnchorSink  # noqa: E402
from gawe.anchoring.rfc3161 import (  # noqa: E402
    AUTHORITIES,
    Rfc3161AnchorSink,
    TimestampError,
    token_from_reply,
    verify_token,
)
from gawe.core.anchor import Anchorer, AnchorRecord, anchor_digest  # noqa: E402
from gawe.core.audit import AnchorPoint, AuditLog, InMemoryAuditStore  # noqa: E402
from gawe.core.runs import RunRecord  # noqa: E402
from gawe.core.statemachine import RunStatus  # noqa: E402
from gawe.core.telemetry import InMemoryTelemetry  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "tsa"
META: dict[str, Any] = json.loads((FIXTURES / "record.json").read_text())
DIGEST = bytes.fromhex(META["digest"])
NONCE: int = META["nonce"]
NAMES = sorted(AUTHORITIES)


def reply(name: str) -> bytes:
    return (FIXTURES / f"{name}.tsr").read_bytes()


def token(name: str) -> bytes:
    return token_from_reply(reply(name))


def roots(name: str) -> list[Any]:
    return AUTHORITIES[name].pinned_roots()


def fixture_record(sink: str = "rfc3161") -> AnchorRecord:
    point = AnchorPoint(seq=META["seq"], head=META["head"])
    return AnchorRecord(
        tenant_id=META["tenant_id"],
        run_id=META["run_id"],
        point=point,
        run_status="completed",
        anchored_at=datetime(2026, 10, 30, tzinfo=UTC),
        digest=anchor_digest(tenant_id=META["tenant_id"], run_id=META["run_id"], point=point),
        sink=sink,
    )


class FakeAuthority:
    """Answers with a recorded reply and counts how often it was asked."""

    def __init__(self, name: str = "digicert", answer: bytes | None = None) -> None:
        self.answer = answer if answer is not None else reply(name)
        self.calls: list[tuple[str, bytes]] = []

    def __call__(self, url: str, body: bytes) -> bytes:
        self.calls.append((url, body))
        return self.answer


def make_sink(tmp_path: Path, name: str = "digicert", **kw: Any) -> Rfc3161AnchorSink:
    post = kw.pop("post", FakeAuthority(name))
    return Rfc3161AnchorSink(
        tmp_path / "ts.jsonl", authority=name, post=post, nonce=lambda: NONCE, **kw
    )


def resign(token_der: bytes, change: Any) -> bytes:
    """Load a token, let `change` alter it, and re-encode it (the signature is not redone)."""
    info = cms.ContentInfo.load(token_der)
    change(info["content"])
    return bytes(
        cms.ContentInfo({"content_type": "signed_data", "content": info["content"]}).dump(
            force=True
        )
    )


def test_fixture_matches_the_record_it_claims() -> None:
    assert fixture_record().digest == META["digest"] and fixture_record().digest_ok()


# ------------------------------------------------------------------- tokens


@pytest.mark.parametrize("name", NAMES)
def test_a_recorded_token_verifies_against_its_pinned_root(name: str) -> None:
    info = verify_token(token(name), digest=DIGEST, roots=roots(name), nonce=NONCE)
    assert info.gen_time.tzinfo is not None and info.serial > 0 and info.policy


@pytest.mark.parametrize("name", NAMES)
def test_a_token_for_another_digest_is_refused(name: str) -> None:
    with pytest.raises(TimestampError, match="does not cover this anchor"):
        verify_token(token(name), digest=bytes(32), roots=roots(name))


@pytest.mark.parametrize("name", NAMES)
def test_a_token_answering_another_request_is_refused(name: str) -> None:
    with pytest.raises(TimestampError, match="does not answer this request"):
        verify_token(token(name), digest=DIGEST, roots=roots(name), nonce=NONCE + 1)


@pytest.mark.parametrize(("name", "other"), [("digicert", "sectigo"), ("sectigo", "digicert")])
def test_a_token_that_does_not_chain_to_a_pinned_root_is_refused(name: str, other: str) -> None:
    with pytest.raises(TimestampError, match="does not chain to a pinned root"):
        verify_token(token(name), digest=DIGEST, roots=roots(other))
    with pytest.raises(TimestampError, match="does not chain to a pinned root"):
        verify_token(token(name), digest=DIGEST, roots=[])


@pytest.mark.parametrize("name", NAMES)
def test_a_tampered_signature_is_refused(name: str) -> None:
    def flip(signed_data: Any) -> None:
        signer = signed_data["signer_infos"][0]
        signature = bytearray(signer["signature"].native)
        signature[-1] ^= 0x01
        signer["signature"] = core.OctetString(bytes(signature))

    with pytest.raises(TimestampError, match="signature does not verify"):
        verify_token(resign(token(name), flip), digest=DIGEST, roots=roots(name))


@pytest.mark.parametrize("name", NAMES)
def test_altered_timestamp_content_is_refused(name: str) -> None:
    def back_date(signed_data: Any) -> None:
        encap = signed_data["encap_content_info"]
        info = tsp.TSTInfo.load(bytes(encap["content"].contents))
        info["gen_time"] = datetime(2020, 1, 1, tzinfo=UTC)
        encap["content"] = core.ParsableOctetString(info.dump(force=True))

    with pytest.raises(TimestampError, match="do not match the token content"):
        verify_token(resign(token(name), back_date), digest=DIGEST, roots=roots(name))


@pytest.mark.parametrize("name", NAMES)
def test_a_token_without_its_signer_certificate_is_refused(name: str) -> None:
    def strip(signed_data: Any) -> None:
        signed_data["certificates"] = cms.CertificateSet([])

    with pytest.raises(TimestampError, match="include its signer's certificate"):
        verify_token(resign(token(name), strip), digest=DIGEST, roots=roots(name))


def test_a_certificate_outside_its_validity_at_the_stamped_time_is_refused() -> None:
    info = cms.ContentInfo.load(token("digicert"))["content"]
    from cryptography import x509

    certs = [x509.load_der_x509_certificate(c.chosen.dump()) for c in info["certificates"]]
    signer = next(c for c in certs if "Responder" in c.subject.rfc4514_string())
    too_late = datetime(2099, 1, 1, tzinfo=UTC)
    with pytest.raises(TimestampError, match="not valid at the stamped time"):
        rfc3161._check_chain(signer, certs, roots("digicert"), too_late)


@pytest.mark.parametrize("garbage", [b"", b"\x30\x03\x02\x01\x00", b"not der at all"])
def test_malformed_tokens_are_refused(garbage: bytes) -> None:
    with pytest.raises(TimestampError):
        verify_token(garbage, digest=DIGEST, roots=roots("digicert"))


# -------------------------------------------------------------------- replies


def test_a_refused_request_is_an_error() -> None:
    refused = bytes.fromhex("30053003020102")  # status: rejection, no token
    with pytest.raises(TimestampError, match="did not grant"):
        token_from_reply(refused)


def test_a_granted_reply_without_a_token_is_an_error() -> None:
    with pytest.raises(TimestampError, match="sent no token"):
        token_from_reply(bytes.fromhex("30053003020100"))


@pytest.mark.parametrize("garbage", [b"", b"<html>502 Bad Gateway</html>"])
def test_a_reply_that_is_not_a_timestamp_response_is_an_error(garbage: bytes) -> None:
    with pytest.raises(TimestampError):
        token_from_reply(garbage)


def test_http_post_refuses_an_oversized_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return b"x" * limit

    monkeypatch.setattr(rfc3161.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(TimestampError, match="too large"):
        rfc3161.http_post("http://tsa.invalid", b"q")


# ----------------------------------------------------------------------- sink


@pytest.mark.parametrize("name", NAMES)
def test_put_keeps_the_signed_token_and_check_accepts_it(tmp_path: Path, name: str) -> None:
    authority = FakeAuthority(name)
    sink = make_sink(tmp_path, name, post=authority)
    stored = sink.put(fixture_record())
    assert authority.calls[0][0] == AUTHORITIES[name].url
    assert stored.evidence["tsa"] == name and base64.b64decode(stored.evidence["token"])
    reloaded = Rfc3161AnchorSink(tmp_path / "ts.jsonl", authority=name)
    assert reloaded.get(META["run_id"]) == stored
    check = reloaded.check(stored)
    assert check.ok and check.external and check.timestamped_at is not None


def test_an_anchor_is_never_requested_twice(tmp_path: Path) -> None:
    authority = FakeAuthority()
    sink = make_sink(tmp_path, post=authority)
    first = sink.put(fixture_record())
    assert sink.put(fixture_record()) == first and len(authority.calls) == 1


def test_a_reply_that_does_not_answer_our_request_is_not_stored(tmp_path: Path) -> None:
    sink = Rfc3161AnchorSink(tmp_path / "ts.jsonl", post=FakeAuthority(), nonce=lambda: NONCE + 1)
    with pytest.raises(TimestampError):
        sink.put(fixture_record())
    assert sink.get(META["run_id"]) is None


def test_an_authority_that_cannot_be_reached_never_breaks_the_run(tmp_path: Path) -> None:
    def unreachable(url: str, body: bytes) -> bytes:
        raise OSError("connection refused")

    store = InMemoryAuditStore()
    log = AuditLog(store)
    event = log.record("run1", "run.completed", "gawe.orchestrator")
    run = RunRecord(
        run_id="run1",
        tenant_id="t1",
        acting_user_id="alice",
        objective="x",
        profile="p",
        status=RunStatus.COMPLETED,
        created_at=event.ts,
    )
    telemetry = InMemoryTelemetry()
    sinks = [make_sink(tmp_path, post=unreachable), FileAnchorSink(tmp_path / "f.jsonl")]
    written = Anchorer(log, sinks, telemetry=telemetry).anchor_run(run)
    assert [r.sink for r in written] == ["file"]
    assert ("gawe.anchor.failures", 1, {"sink": "rfc3161"}) in telemetry.counts


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        ({}, "no timestamp in the record"),
        ({"tsa": "nobody", "token": "AA=="}, "no timestamp in the record"),
        ({"tsa": "digicert", "token": "%%%"}, "timestamp is not base64"),
        ({"tsa": "sectigo"}, "no timestamp in the record"),
    ],
)
def test_check_refuses_missing_or_broken_evidence(
    tmp_path: Path, evidence: dict[str, str], reason: str
) -> None:
    sink = make_sink(tmp_path)
    check = sink.check(replace(fixture_record(), evidence=evidence))
    assert not check.ok and check.external and check.reason == reason


def test_check_refuses_a_token_moved_to_another_record(tmp_path: Path) -> None:
    stored = make_sink(tmp_path).put(fixture_record())
    other = AnchorPoint(seq=META["seq"] + 1, head=META["head"])
    moved = replace(
        stored,
        point=other,
        digest=anchor_digest(tenant_id=stored.tenant_id, run_id=stored.run_id, point=other),
    )
    check = make_sink(tmp_path).check(moved)
    assert not check.ok and check.reason == "token does not cover this anchor"


def test_check_refuses_a_record_whose_digest_was_edited(tmp_path: Path) -> None:
    stored = make_sink(tmp_path).put(fixture_record())
    check = make_sink(tmp_path).check(replace(stored, run_id="another-run"))
    assert not check.ok and check.reason == "digest does not match the record"


def test_unknown_authority_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Rfc3161AnchorSink(tmp_path / "ts.jsonl", authority="nobody")


# --------------------------------------------------------------------- config


def test_config_builds_the_timestamp_sink(tmp_path: Path) -> None:
    env = {"GAWE_ANCHOR_SINKS": "file,rfc3161", "GAWE_ANCHOR_DIR": str(tmp_path)}
    file_sink, ts = sinks_from_env(env)
    assert isinstance(ts, Rfc3161AnchorSink) and ts._authority == "digicert"
    assert ts._path == tmp_path / "anchors-rfc3161.jsonl"
    (ts,) = sinks_from_env({**env, "GAWE_ANCHOR_SINKS": "rfc3161", "GAWE_ANCHOR_TSA": "Sectigo"})
    assert isinstance(ts, Rfc3161AnchorSink) and ts._authority == "sectigo"


def test_config_refuses_an_unknown_authority() -> None:
    with pytest.raises(AnchorConfigError):
        sinks_from_env({"GAWE_ANCHOR_SINKS": "rfc3161", "GAWE_ANCHOR_TSA": "nobody"})
