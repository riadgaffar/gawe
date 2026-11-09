"""IT access requests: a small second workflow showing the engine is not contract-specific.

Run with `GAWE_APP=gawe.apps.it_access:bundle`.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gawe.api.wiring import AppBundle
from gawe.apps.it_access.reader_schema import PROFILE
from gawe.apps.it_access.tools import MANIFESTS, POSTCONDITIONS, implementations
from gawe.connectors.base import Ledger
from gawe.connectors.mock_iam import MockIAM
from gawe.core.runs import RunRecord

HERE = Path(__file__).parent
PROFILE_NAME = "it_access"
TENANT_VARIABLES: Mapping[str, Any] = {"tenant": {"internal_domains": ["acme.example"]}}


def bundle(demo_dir: Path | None = None) -> AppBundle:
    directory = demo_dir or Path(os.environ.get("GAWE_DEMO_DIR", ".demo-data"))
    ledger = Ledger(directory / "ledger.jsonl")
    iam = MockIAM.from_seed(HERE / "samples" / "iam_seed.json", ledger)
    return AppBundle(
        manifests=MANIFESTS,
        tools=implementations(iam),
        policy_rules=HERE / "policies.yaml",
        profiles=frozenset({PROFILE_NAME}),
        reader_profile=PROFILE,
        variables=_variables,
        postconditions=POSTCONDITIONS,
    )


def _variables(run: RunRecord) -> Mapping[str, Any]:
    return TENANT_VARIABLES
