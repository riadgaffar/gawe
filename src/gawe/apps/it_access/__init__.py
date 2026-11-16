"""IT access requests: a small second workflow showing the engine is not contract-specific.

Run with `GAWE_APP=gawe.apps.it_access:bundle`.
"""

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gawe.api.wiring import AppBundle
from gawe.apps.it_access.grant_preparation import DEFAULT_LIFETIME_HOURS, GrantPreparer
from gawe.apps.it_access.reader_schema import PROFILE
from gawe.apps.it_access.tools import MANIFESTS, POSTCONDITIONS, implementations
from gawe.apps.readback import operation_adapter
from gawe.connectors.base import Ledger
from gawe.connectors.mock_iam import MockIAM
from gawe.core.policy.provenance import PolicySource, PolicyVariable
from gawe.core.policy.rules import email_domain
from gawe.core.readback import ReadbackAdapter
from gawe.core.runs import RunRecord

HERE = Path(__file__).parent
PROFILE_NAME = "it_access"
MAX_GRANT_LIFETIME_HOURS = 30 * 24
TENANT_VARIABLES: Mapping[str, Any] = {
    "tenant": {
        "internal_domains": ["acme.example"],
        "max_grant_lifetime_hours": MAX_GRANT_LIFETIME_HOURS,
    }
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def bundle(
    demo_dir: Path | None = None,
    *,
    iam: MockIAM | None = None,
    grant_lifetime_hours: int = DEFAULT_LIFETIME_HOURS,
    clock: Callable[[], datetime] = _utc_now,
) -> AppBundle:
    directory = demo_dir or Path(os.environ.get("GAWE_DEMO_DIR", ".demo-data"))
    ledger = Ledger(directory / "ledger.jsonl")
    iam = iam or MockIAM.from_seed(HERE / "samples" / "iam_seed.json", ledger)
    return AppBundle(
        manifests=MANIFESTS,
        tools=implementations(iam),
        readbacks=readbacks(iam),
        preparers={
            ("iam.grant_access", "2.0.0"): GrantPreparer(
                iam, lifetime_hours=grant_lifetime_hours, clock=clock
            )
        },
        conditional_writes=iam.CONDITIONAL_WRITES,
        revokers={("iam.grant_access", "2.0.0"): iam},
        policy_rules=HERE / "policies.yaml",
        profiles=frozenset({PROFILE_NAME}),
        reader_profile=PROFILE,
        variables=_variables,
        postconditions=POSTCONDITIONS,
        policy_sources={
            "tenant_config": PolicySource(kind="configuration", resource="config.tenant"),
            "reader_findings": PolicySource(kind="findings", resource="run.findings"),
        },
        policy_variables={
            "tenant.internal_domains": PolicyVariable(dependencies=("tenant_config",)),
            "tenant.max_grant_lifetime_hours": PolicyVariable(dependencies=("tenant_config",)),
            "request.requester_email": PolicyVariable(dependencies=("reader_findings",)),
        },
    )


def readbacks(iam: MockIAM) -> dict[tuple[str, str], ReadbackAdapter]:
    """Version-specific read-back. 1.0.0 reads the older store, for earlier dispatches only."""
    return {
        ("iam.grant_access", "1.0.0"): operation_adapter(
            iam.legacy_operations,
            ("email", "system", "level"),
            ("grant_id", "email", "system", "level"),
        ),
        ("iam.grant_access", "2.0.0"): operation_adapter(
            iam.operations,
            ("email", "system", "level", "record", "expires_at", "lifetime_hours"),
            ("grant_id", "email", "system", "level"),
        ),
    }


def request_variables(findings: Mapping[str, Any] | None) -> dict[str, str]:
    """The reported requester, unchanged; unusable findings cannot authorize a grant.

    This binds the Planner to the Reader's report, not to an independently verified identity.
    """
    requester = (findings or {}).get("requester_email")
    if not isinstance(requester, str) or email_domain(requester) is None:
        return {}
    return {"requester_email": requester}


def _variables(run: RunRecord | None) -> Mapping[str, Any]:
    return {
        **TENANT_VARIABLES,
        "request": request_variables(run.findings if run is not None else None),
    }
