"""Contract Review: read a contract, record a risk assessment, email counsel, send for signature.

Run with `GAWE_APP=gawe.apps.contract_review:bundle`. Mock connectors write their effects to
`$GAWE_DEMO_DIR/ledger.jsonl` (default `.demo-data/`), so you can see exactly what happened and
that a retried call did not repeat it.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gawe.api.wiring import AppBundle
from gawe.apps.contract_review.reader_schema import PROFILE
from gawe.apps.contract_review.tools import MANIFESTS, POSTCONDITIONS, implementations
from gawe.connectors.base import Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_email import MockEmail
from gawe.connectors.mock_esign import MockESign
from gawe.core.runs import RunRecord

HERE = Path(__file__).parent
PROFILE_NAME = "contract_review"
TENANT_VARIABLES: Mapping[str, Any] = {
    "tenant": {
        "internal_domains": ["acme.example"],
        "blocked_domains": ["competitor.example", "evil.example"],
    }
}


def bundle(demo_dir: Path | None = None, *, email: MockEmail | None = None) -> AppBundle:
    directory = demo_dir or Path(os.environ.get("GAWE_DEMO_DIR", ".demo-data"))
    ledger = Ledger(directory / "ledger.jsonl")
    clm = MockCLM.from_seed(HERE / "samples" / "clm_seed.json", ledger)
    return AppBundle(
        manifests=MANIFESTS,
        tools=implementations(clm, email or MockEmail(ledger), MockESign(ledger)),
        policy_rules=HERE / "policies.yaml",
        profiles=frozenset({PROFILE_NAME}),
        reader_profile=PROFILE,
        variables=_variables,
        postconditions=POSTCONDITIONS,
    )


def _variables(run: RunRecord) -> Mapping[str, Any]:
    return TENANT_VARIABLES
