"""Signed timestamps (RFC 3161) from an outside authority: the external anchor sink.

When a run ends, the anchor record's digest (a SHA-256 over tenant, run, sequence number and
head; never run data) is sent to a timestamp authority. Its signed reply proves that digest
existed at the stamped time, and nobody here can forge or back-date it. Replies are kept in a
local JSON-lines file next to the record; anyone can re-check them offline.

Checking a token (`verify_token`):
* the token covers exactly this record's digest, with SHA-256;
* the signed attributes name the token content and carry its correct digest;
* the signature verifies with the signer certificate, which is marked for time stamping only;
* the signer's chain leads to a root pinned in `tsa_certs/` for that authority (not the system
  trust store), each certificate valid at the stamped time.

Certificates are checked as of the stamped time, not today, so evidence stays checkable after
they expire. Revocation is not checked.

Needs the optional extra: pip install -e ".[anchor]".
"""

import base64
import hashlib
import secrets
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from asn1crypto import cms, core, tsp
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID

from gawe.anchoring.file_sink import FileAnchorSink
from gawe.core.anchor import AnchorRecord, EvidenceCheck

CERT_DIR = Path(__file__).parent / "tsa_certs"
_MAX_REPLY_BYTES = 64 * 1024
_MAX_CHAIN = 6
_HASHES: dict[str, Callable[[], hashes.HashAlgorithm]] = {
    "sha256": hashes.SHA256,
    "sha384": hashes.SHA384,
    "sha512": hashes.SHA512,
}


@dataclass(frozen=True)
class Authority:
    url: str
    roots: tuple[str, ...]  # PEM files in tsa_certs/

    def pinned_roots(self) -> list[x509.Certificate]:
        certs: list[x509.Certificate] = []
        for name in self.roots:
            certs.extend(x509.load_pem_x509_certificates((CERT_DIR / name).read_bytes()))
        return certs


AUTHORITIES: Mapping[str, Authority] = {
    "digicert": Authority("http://timestamp.digicert.com", ("digicert_trusted_root_g4.pem",)),
    "sectigo": Authority("http://timestamp.sectigo.com", ("usertrust_rsa_ca.pem",)),
}
DEFAULT_AUTHORITY = "digicert"

Post = Callable[[str, bytes], bytes]


class TimestampError(Exception):
    """A timestamp reply or token could not be obtained or does not check out.

    Messages are fixed phrases; they never carry token bytes or hashes.
    """


@dataclass(frozen=True)
class TokenInfo:
    gen_time: datetime
    serial: int
    policy: str


def build_request(digest: bytes, nonce: int) -> bytes:
    request = tsp.TimeStampReq(
        {
            "version": 1,
            "message_imprint": {
                "hash_algorithm": {"algorithm": "sha256"},
                "hashed_message": digest,
            },
            "nonce": nonce,
            "cert_req": True,
        }
    )
    return bytes(request.dump())


def token_from_reply(reply: bytes) -> bytes:
    """The DER time-stamp token inside an authority's reply."""
    try:
        # Read the parts by position: asn1crypto's TimeStampResp insists on a token, but a refusal
        # carries only a status.
        outer = core.Sequence.load(reply)
        status = tsp.PKIStatusInfo.load(outer[0].dump())["status"].native
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise TimestampError("reply is not a timestamp response") from exc
    if status not in ("granted", "granted_with_mods"):
        raise TimestampError("the authority did not grant the timestamp")
    try:
        token = cms.ContentInfo.load(outer[1].dump())
        return bytes(token.dump())
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise TimestampError("the authority granted the timestamp but sent no token") from exc


def http_post(url: str, body: bytes, *, timeout: float = 10.0) -> bytes:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/timestamp-query"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed URLs)
        data: bytes = response.read(_MAX_REPLY_BYTES + 1)
    if len(data) > _MAX_REPLY_BYTES:
        raise TimestampError("reply is too large")
    return data


def verify_token(
    token_der: bytes,
    *,
    digest: bytes,
    roots: list[x509.Certificate],
    nonce: int | None = None,
) -> TokenInfo:
    """Check a time-stamp token for `digest` against pinned `roots`. Raises TimestampError."""
    try:
        content_info = cms.ContentInfo.load(token_der)
        signed_data = content_info["content"]
        encap = signed_data["encap_content_info"]
        if content_info["content_type"].native != "signed_data":
            raise TimestampError("token is not signed data")
        if encap["content_type"].native != "tst_info":
            raise TimestampError("token does not hold timestamp information")
        content = bytes(encap["content"].contents)
        tst_info = tsp.TSTInfo.load(content)
        imprint = tst_info["message_imprint"]
        algorithm = imprint["hash_algorithm"]["algorithm"].native
        hashed = imprint["hashed_message"].native
        gen_time: datetime = tst_info["gen_time"].native
        token_nonce = tst_info["nonce"].native
        info = TokenInfo(
            gen_time=gen_time,
            serial=int(tst_info["serial_number"].native),
            policy=str(tst_info["policy"].native),
        )
        signer_infos = signed_data["signer_infos"]
        certificates = [
            x509.load_der_x509_certificate(choice.chosen.dump())
            for choice in (signed_data["certificates"] or [])
            if choice.name == "certificate"
        ]
    except TimestampError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise TimestampError("token is malformed") from exc

    if algorithm != "sha256" or hashed != digest:
        raise TimestampError("token does not cover this anchor")
    if nonce is not None and token_nonce != nonce:
        raise TimestampError("token does not answer this request")
    if len(signer_infos) != 1:
        raise TimestampError("token must have exactly one signer")
    signer_info = signer_infos[0]
    signer = _signer_certificate(signer_info, certificates)
    _check_signed_attributes(signer_info, content, signer)
    _check_signature(signer_info, signer)
    _check_chain(signer, certificates, roots, info.gen_time)
    return info


def _signer_certificate(signer_info: Any, certificates: list[x509.Certificate]) -> x509.Certificate:
    sid = signer_info["sid"]
    for cert in certificates:
        if sid.name == "issuer_and_serial_number":
            issuer_serial = sid.chosen
            if (
                cert.serial_number == issuer_serial["serial_number"].native
                and cert.issuer.public_bytes() == issuer_serial["issuer"].dump()
            ):
                return cert
        elif sid.name == "subject_key_identifier":
            try:
                ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            except x509.ExtensionNotFound:
                continue
            if ski.value.digest == sid.chosen.native:
                return cert
    raise TimestampError("token does not include its signer's certificate")


def _check_signed_attributes(signer_info: Any, content: bytes, signer: x509.Certificate) -> None:
    attrs = signer_info["signed_attrs"]
    if not attrs:
        raise TimestampError("token has no signed attributes")
    hash_name = signer_info["digest_algorithm"]["algorithm"].native
    if hash_name not in _HASHES:
        raise TimestampError("token uses an unsupported digest")
    values: dict[str, Any] = {attr["type"].native: attr["values"][0] for attr in attrs}
    if values.get("content_type") is None or values["content_type"].native != "tst_info":
        raise TimestampError("signed attributes name the wrong content")
    expected = hashlib.new(hash_name, content).digest()
    if values.get("message_digest") is None or values["message_digest"].native != expected:
        raise TimestampError("signed attributes do not match the token content")
    # ESS signing-certificate binds the signature to one certificate, when present.
    for name, default_hash in (
        ("signing_certificate_v2", "sha256"),
        ("signing_certificate", "sha1"),
    ):
        if name in values:
            cert_id = values[name]["certs"][0]
            if name == "signing_certificate_v2":
                default_hash = cert_id["hash_algorithm"]["algorithm"].native
            cert_hash = hashlib.new(default_hash, signer.public_bytes(Encoding.DER)).digest()
            if cert_id["cert_hash"].native != cert_hash:
                raise TimestampError("signed attributes name a different certificate")


def _check_signature(signer_info: Any, signer: x509.Certificate) -> None:
    signed = bytearray(signer_info["signed_attrs"].dump())
    signed[0] = 0x31  # the signature covers the attributes as a SET, not as [0] IMPLICIT
    signature = signer_info["signature"].native
    hash_algorithm = _HASHES[signer_info["digest_algorithm"]["algorithm"].native]()
    scheme = signer_info["signature_algorithm"].signature_algo
    key = signer.public_key()
    try:
        if isinstance(key, rsa.RSAPublicKey) and scheme == "rsassa_pkcs1v15":
            key.verify(signature, bytes(signed), padding.PKCS1v15(), hash_algorithm)
        elif isinstance(key, rsa.RSAPublicKey) and scheme == "rsassa_pss":
            params = signer_info["signature_algorithm"]["parameters"]
            pss = padding.PSS(
                mgf=padding.MGF1(hash_algorithm), salt_length=params["salt_length"].native
            )
            key.verify(signature, bytes(signed), pss, hash_algorithm)
        elif isinstance(key, ec.EllipticCurvePublicKey) and scheme == "ecdsa":
            key.verify(signature, bytes(signed), ec.ECDSA(hash_algorithm))
        else:
            raise TimestampError("token uses an unsupported signature scheme")
    except InvalidSignature as exc:
        raise TimestampError("token signature does not verify") from exc

    try:
        usage = signer.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound as exc:
        raise TimestampError("signer certificate is not for time stamping") from exc
    if not usage.critical or list(usage.value) != [ExtendedKeyUsageOID.TIME_STAMPING]:
        raise TimestampError("signer certificate is not for time stamping")


def _check_chain(
    signer: x509.Certificate,
    certificates: list[x509.Certificate],
    roots: list[x509.Certificate],
    at: datetime,
) -> None:
    pinned = {root.fingerprint(hashes.SHA256()) for root in roots}
    current = signer
    for _ in range(_MAX_CHAIN):
        if not current.not_valid_before_utc <= at <= current.not_valid_after_utc:
            raise TimestampError("a certificate was not valid at the stamped time")
        if current.fingerprint(hashes.SHA256()) in pinned:
            return
        issuer = _issuer_of(current, roots + certificates)
        if issuer is None:
            raise TimestampError("signer does not chain to a pinned root")
        if current is not signer and not _is_ca(current):
            raise TimestampError("an intermediate certificate is not a CA")
        if not _is_ca(issuer):
            raise TimestampError("an issuing certificate is not a CA")
        current = issuer
    raise TimestampError("certificate chain is too long")


def _issuer_of(
    cert: x509.Certificate, candidates: list[x509.Certificate]
) -> x509.Certificate | None:
    for candidate in candidates:
        if candidate.subject != cert.issuer or candidate == cert:
            continue
        try:
            cert.verify_directly_issued_by(candidate)
        except (ValueError, TypeError, InvalidSignature):
            continue
        return candidate
    return None


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


class Rfc3161AnchorSink(FileAnchorSink):
    """Gets a signed timestamp for each anchor and keeps it in a local JSON-lines file."""

    name = "rfc3161"
    external = True

    def __init__(
        self,
        path: Path,
        *,
        authority: str = DEFAULT_AUTHORITY,
        post: Post = http_post,
        nonce: Callable[[], int] = lambda: secrets.randbits(63),
    ) -> None:
        if authority not in AUTHORITIES:
            raise ValueError(f"unknown timestamp authority {authority!r}")
        super().__init__(path)
        self._authority = authority
        self._post = post
        self._nonce = nonce

    def put(self, record: AnchorRecord) -> AnchorRecord:
        existing = self.get(record.run_id)
        if existing is not None and existing.digest == record.digest:
            return existing  # never ask the authority twice for the same anchor
        authority = AUTHORITIES[self._authority]
        digest = bytes.fromhex(record.digest)
        nonce = self._nonce()
        token = token_from_reply(self._post(authority.url, build_request(digest, nonce)))
        info = verify_token(token, digest=digest, roots=authority.pinned_roots(), nonce=nonce)
        evidence = {
            "tsa": self._authority,
            "token": base64.b64encode(token).decode("ascii"),
            "gen_time": info.gen_time.isoformat(),
            "serial": str(info.serial),
            "policy": info.policy,
        }
        return super().put(replace(record, evidence=evidence))

    def check(self, record: AnchorRecord) -> EvidenceCheck:
        if not record.digest_ok():
            return EvidenceCheck(ok=False, external=True, reason="digest does not match the record")
        evidence = record.evidence
        authority = AUTHORITIES.get(str(evidence.get("tsa", "")))
        if authority is None or not isinstance(evidence.get("token"), str):
            return EvidenceCheck(ok=False, external=True, reason="no timestamp in the record")
        try:
            token = base64.b64decode(evidence["token"], validate=True)
            info = verify_token(
                token, digest=bytes.fromhex(record.digest), roots=authority.pinned_roots()
            )
        except (TimestampError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, TimestampError) else "timestamp is not base64"
            return EvidenceCheck(ok=False, external=True, reason=reason)
        return EvidenceCheck(ok=True, external=True, timestamped_at=info.gen_time)
