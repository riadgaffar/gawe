"""Records one live timestamp reply per authority for the offline tests in test_rfc3161.py.

Sends only the SHA-256 digest of a fixed, made-up anchor record (tenant "fixture"). Run it again
only to refresh the fixtures, for example after an authority changes its certificates:

    python scripts/record_tsa_fixture.py
"""

import json
from pathlib import Path

from gawe.anchoring.rfc3161 import AUTHORITIES, build_request, http_post
from gawe.core.anchor import anchor_digest
from gawe.core.audit import AnchorPoint
from gawe.core.canonical import sha256_hex

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "tsa"
TENANT, RUN = "fixture", "fixture-run"
POINT = AnchorPoint(seq=7, head=sha256_hex(b"gawe tsa fixture head"))
NONCE = 4242424242


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    digest = anchor_digest(tenant_id=TENANT, run_id=RUN, point=POINT)
    meta = {"tenant_id": TENANT, "run_id": RUN, "seq": POINT.seq, "head": POINT.head}
    meta |= {"digest": digest, "nonce": NONCE}
    (OUT / "record.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    for name, authority in AUTHORITIES.items():
        reply = http_post(authority.url, build_request(bytes.fromhex(digest), NONCE))
        (OUT / f"{name}.tsr").write_bytes(reply)
        print(f"{name}: {len(reply)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
