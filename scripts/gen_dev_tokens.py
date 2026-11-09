"""Creates API tokens for the people in scripts/seed_dev.sql.

Writes `.dev-tokens.json` (only SHA-256 hashes; point GAWE_API_TOKENS_FILE at it) and prints each
raw token ONCE. Refuses to overwrite an existing file, so tokens you already handed out keep
working; delete the file to start over.
"""

import json
import sys
from pathlib import Path

from gawe.api.auth import hash_token, new_token

TENANT = "00000000-0000-0000-0000-00000000aa01"
PEOPLE = [
    ("alice", "00000000-0000-0000-0000-00000000aa11", ["case_manager", "it_agent"]),
    ("bob", "00000000-0000-0000-0000-00000000aa12", ["legal_reviewer", "security_admin"]),
    ("root", "00000000-0000-0000-0000-00000000aa13", ["admin"]),
    ("fiona", "00000000-0000-0000-0000-00000000aa14", ["finance_approver"]),
]
OUT = Path(".dev-tokens.json")


def main() -> int:
    if OUT.exists():
        print(f"{OUT} already exists; delete it first to create new tokens", file=sys.stderr)
        return 1
    entries = []
    for name, user_id, roles in PEOPLE:
        token = new_token()
        entries.append(
            {
                "sha256": hash_token(token),
                "tenant_id": TENANT,
                "user_id": user_id,
                "roles": roles,
            }
        )
        print(f"{name:6} {token}")
    OUT.write_text(json.dumps({"tokens": entries}, indent=2) + "\n", encoding="utf-8")
    OUT.chmod(0o600)
    print(f"\nwrote {OUT} (hashes only). Keep the tokens above; they are not stored anywhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
