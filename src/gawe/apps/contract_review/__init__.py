"""Contract Review: read a contract, record a risk assessment, email counsel, send for signature.

Policy sees two values derived per run (`review.*`, see `review_variables`), so a "low risk"
assessment needs legal approval when the Reader's own findings contradict it, or when the deal is
large. The deal value comes from the contract system, which a document cannot change.

Run with `GAWE_APP=gawe.apps.contract_review:bundle`. Mock connectors write their effects to
`$GAWE_DEMO_DIR/ledger.jsonl` (default `.demo-data/`), so you can see exactly what happened and
that a retried call did not repeat it.
"""

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from gawe.api.wiring import AppBundle
from gawe.apps.contract_review.reader_schema import PROFILE
from gawe.apps.contract_review.tools import MANIFESTS, POSTCONDITIONS, implementations
from gawe.connectors.base import Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_email import MockEmail
from gawe.connectors.mock_esign import MockESign
from gawe.core.policy.provenance import PolicySource, PolicyVariable
from gawe.core.runs import RunRecord

HERE = Path(__file__).parent
LARGE_DEAL_THRESHOLD = 100_000  # the same line the signature rule draws for finance approval
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
        variables=_variables_for(clm),
        postconditions=POSTCONDITIONS,
        policy_sources={
            "tenant_config": PolicySource(kind="configuration", resource="config.tenant"),
            "reader_findings": PolicySource(kind="findings", resource="run.findings"),
            "deal_values": PolicySource(kind="resource", resource="clm.contract.deal_value"),
        },
        policy_variables={
            "tenant.internal_domains": PolicyVariable(dependencies=("tenant_config",)),
            "tenant.blocked_domains": PolicyVariable(dependencies=("tenant_config",)),
            "review.unsupported_levels": PolicyVariable(dependencies=("reader_findings",)),
            "review.large_deals": PolicyVariable(dependencies=("deal_values",)),
        },
    )


def _variables_for(clm: MockCLM) -> Callable[[RunRecord | None], Mapping[str, Any]]:
    def variables(run: RunRecord | None) -> Mapping[str, Any]:
        findings = run.findings if run is not None else None
        return {**TENANT_VARIABLES, "review": review_variables(findings, clm.deal_values())}

    return variables


def review_variables(
    findings: Mapping[str, Any] | None, deal_values: Mapping[str, float]
) -> dict[str, list[str]]:
    """Values the downgrade rules compare a CRM assessment with.

    - `unsupported_levels`: ["low"] when the Reader's findings hold a high-severity flagged clause
      (text aimed at an AI reviewer is reported as one), so "low" contradicts them; else [].
      Catches a Reader fooled partway, not one that reports "low" with nothing flagged.
    - `large_deals`: contract ids whose deal value in the contract system is over the threshold.
      Catches a fully fooled "low" on a large deal; a small deal is a policy choice to allow.
    """
    clauses = (findings or {}).get("flagged_clauses") or []
    contradicted = any(isinstance(c, Mapping) and c.get("severity") == "high" for c in clauses)
    return {
        "unsupported_levels": ["low"] if contradicted else [],
        "large_deals": sorted(
            cid for cid, value in deal_values.items() if value > LARGE_DEAL_THRESHOLD
        ),
    }
