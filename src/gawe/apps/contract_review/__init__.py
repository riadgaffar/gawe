"""Contract Review: read a contract, record a risk assessment, email counsel, send for signature.

Policy sees two values derived per run (`review.*`, see `review_variables`), so a "low risk"
assessment needs legal approval when the Reader's own findings contradict it, or when the deal is
large. The deal value comes from the contract system, which a document cannot change. Since M24 it
also sees the contract's earlier ratings (`history.*`, see `rating_history`), so a rating lower
than the contract's last needs legal, whichever run made the last one.

Run with `GAWE_APP=gawe.apps.contract_review:bundle`. Mock connectors write their effects to
`$GAWE_DEMO_DIR/ledger.jsonl` (default `.demo-data/`), so you can see exactly what happened and
that a retried call did not repeat it.
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from gawe.api.wiring import AppBundle
from gawe.apps.contract_review.email_preparation import EmailPreparer
from gawe.apps.contract_review.reader_schema import PROFILE
from gawe.apps.contract_review.record_preparation import (
    AssessmentPreparer,
    ContractRecordPreparer,
    ContractSnapshotPreparer,
    RestorePreparer,
)
from gawe.apps.contract_review.tools import (
    MANIFESTS,
    NO_RATING,
    POSTCONDITIONS,
    TOOL_SCOPES,
    WRITE_SCOPES,
    implementations,
)
from gawe.apps.readback import email_adapter, operation_adapter
from gawe.apps.refusal_observers import contracts as refusal_observers
from gawe.connectors.base import Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_directory import MockDirectory
from gawe.connectors.mock_email import MockEmail
from gawe.connectors.mock_esign import MockESign
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.history import HistoryEntry, HistorySubject
from gawe.core.policy.provenance import PolicySource, PolicyVariable
from gawe.core.preparation import Preparer
from gawe.core.readback import ReadbackAdapter, compare
from gawe.core.record_changes import RecordSnapshot
from gawe.core.records import StepRecord
from gawe.core.runs import RunRecord
from gawe.core.statemachine import StepStatus
from gawe.core.undo import Undoable

HERE = Path(__file__).parent
LARGE_DEAL_THRESHOLD = 100_000  # the same line the signature rule draws for finance approval
PROFILE_NAME = "contract_review"
ASSESSMENT = "crm.write_assessment"
RATINGS = ("low", "medium", "high")  # lowest first
ASSESSMENT_3 = (ASSESSMENT, "3.0.0")
ASSESSMENT_4 = (ASSESSMENT, "4.0.0")


def _prior(args: Mapping[str, Any]) -> dict[str, str] | None:
    prior = args["prior"]
    if prior["risk_level"] == NO_RATING:
        return None
    return {"risk_level": str(prior["risk_level"]), "summary": str(prior["summary"])}


# M26: a rating can be undone. The record comes from the prepared, hashed arguments.
UNDOABLE = {
    ASSESSMENT_3: Undoable(
        subject=lambda args: {"contract_id": str(args["contract_id"])},
        prior=_prior,
        bound_version=lambda args: str(args["record"]["contract_version"]),
    )
}
UNDOABLE[ASSESSMENT_4] = replace(
    UNDOABLE[ASSESSMENT_3], snapshots=lambda args: (_captured_contract(args),)
)
TENANT_VARIABLES: Mapping[str, Any] = {
    "tenant": {
        "internal_domains": ["acme.example"],
        "blocked_domains": ["competitor.example", "evil.example"],
    }
}


def bundle(
    demo_dir: Path | None = None, *, email: MockEmail | None = None, clm: MockCLM | None = None
) -> AppBundle:
    directory = demo_dir or Path(os.environ.get("GAWE_DEMO_DIR", ".demo-data"))
    ledger = Ledger(directory / "ledger.jsonl")
    clm = clm or MockCLM.from_seed(HERE / "samples" / "clm_seed.json", ledger)
    mail_directory = MockDirectory.from_seed(
        directory, Path(__file__).parents[2] / "connectors/seeds/directory.json"
    )
    mail = email or MockEmail(ledger)
    mail.configure(mail_directory)
    esign = MockESign(ledger, clm)
    assert mail.server is not None
    return AppBundle(
        manifests=MANIFESTS,
        refusal_observers=refusal_observers(clm, MANIFESTS),
        tools=implementations(clm, mail, esign),
        readbacks=readbacks(clm, esign, mail),
        preparers=preparers(clm, mail_directory),
        tool_scopes=TOOL_SCOPES,
        write_scopes=WRITE_SCOPES,
        guarantees={**clm.GUARANTEES, **esign.GUARANTEES, **mail.GUARANTEES},
        undoable=UNDOABLE,
        review_sample=review_sample,
        policy_rules=HERE / "policies.yaml",
        profiles=frozenset({PROFILE_NAME}),
        reader_profile=PROFILE,
        variables=_variables_for(clm),
        action_variables={ASSESSMENT_4: captured_variables, SIGNATURE: captured_variables},
        postconditions=POSTCONDITIONS,
        history={ASSESSMENT: RATING_HISTORY, "crm.restore_assessment": RATING_HISTORY},
        policy_sources={
            "tenant_config": PolicySource(kind="configuration", resource="config.tenant"),
            "reader_findings": PolicySource(kind="findings", resource="run.findings"),
            "deal_values": PolicySource(kind="resource", resource="clm.contract.deal_value"),
            "earlier_ratings": PolicySource(kind="history", resource=f"gawe.history.{ASSESSMENT}"),
        },
        policy_variables={
            "tenant.internal_domains": PolicyVariable(dependencies=("tenant_config",)),
            "tenant.blocked_domains": PolicyVariable(dependencies=("tenant_config",)),
            "review.unsupported_levels": PolicyVariable(dependencies=("reader_findings",)),
            "review.large_deals": PolicyVariable(dependencies=("deal_values",)),
            "history.lower_ratings": PolicyVariable(dependencies=("earlier_ratings",)),
        },
    )


SIGNATURE = ("esign.send_for_signature", "3.0.0")


def signatures_by_hand(demo_dir: Path | None = None) -> AppBundle:
    """The demo app with sending for signature prepare-only (M20): GAWE prepares the envelope and
    a `legal_reviewer` sends it by hand, then marks it. Set
    `GAWE_APP=gawe.apps.contract_review:signatures_by_hand`. The default `bundle` prepares
    nothing."""
    return replace(bundle(demo_dir), prepare_only={SIGNATURE: "legal_reviewer"})


def review_sample(run: RunRecord, step: StepRecord) -> bool:
    """Next-day review candidates: a "low" rating the CRM accepted with no clause flagged.

    Nothing in policy questions these, so a person checks a keyed sample of them. The review
    calls this only for automatic actions that executed. Since M17 every "low" needs legal
    (`low-risk-needs-legal`, provisional), so none executes automatically and this draws nothing;
    it is kept for the day that rule is dropped.
    """
    clauses = (run.findings or {}).get("flagged_clauses")
    return (
        step.tool == "crm.write_assessment"
        and step.resolved_args.get("risk_level") == "low"
        and not clauses
    )


def readbacks(
    clm: MockCLM, esign: MockESign, mail: MockEmail
) -> dict[tuple[str, str], ReadbackAdapter]:
    """Version-specific read-back. 1.0.0 reads the older stores, for earlier dispatches only."""
    assert mail.server is not None

    def restore_readback(key: str, args: Mapping[str, Any], now: Any) -> Any:
        row = clm.operations.read(key, now)
        prior = (
            None
            if args["risk_level"] == NO_RATING
            else {"risk_level": args["risk_level"], "summary": args["summary"]}
        )
        expected = {
            "operation_key": key,
            "contract_id": args["contract_id"],
            "record": args["record"],
            "restored": prior,
        }
        observed = None if row is None else {k: row.get(k) for k in expected}
        output = (
            None
            if row is None
            else {
                "restore_id": row.get("restore_id"),
                "contract_id": row.get("contract_id"),
                "restored_level": (row.get("restored") or {}).get("risk_level"),
            }
        )
        expected["restored"] = sha256_hex(canonical_json(expected["restored"]))
        if observed is not None:
            observed["restored"] = sha256_hex(canonical_json(observed["restored"]))
        return compare(expected, observed, output=output)

    adapters = {
        ("crm.restore_assessment", "1.0.0"): restore_readback,
        ("crm.restore_assessment", "2.0.0"): restore_readback,
        ("crm.write_assessment", "1.0.0"): operation_adapter(
            clm.legacy_operations,
            ("contract_id", "risk_level"),
            ("assessment_id", "contract_id", "risk_level"),
            text_fields=("summary",),
        ),
        ("crm.write_assessment", "2.0.0"): operation_adapter(
            clm.operations,
            ("contract_id", "risk_level", "record"),
            ("assessment_id", "contract_id", "risk_level"),
            text_fields=("summary",),
        ),
        # The same record; the revision after the write is reported, not compared (M26).
        ASSESSMENT_3: operation_adapter(
            clm.operations,
            ("contract_id", "risk_level", "record"),
            ("assessment_id", "contract_id", "risk_level"),
            text_fields=("summary",),
            facts=("result_version",),
        ),
        ("esign.send_for_signature", "1.0.0"): operation_adapter(
            esign.legacy_operations,
            ("contract_id", "signer_email", "deal_value"),
            ("envelope_id", "contract_id"),
        ),
        ("esign.send_for_signature", "2.0.0"): operation_adapter(
            esign.operations,
            ("contract_id", "signer_email", "deal_value", "record"),
            ("envelope_id", "contract_id"),
        ),
        ("email.send", "2.0.0"): email_adapter(mail.server),
    }

    adapters[ASSESSMENT_4] = adapters[ASSESSMENT_3]
    adapters[SIGNATURE] = adapters[("esign.send_for_signature", "2.0.0")]
    return adapters


def preparers(clm: MockCLM, directory: MockDirectory) -> dict[tuple[str, str], Preparer]:
    contract = ContractRecordPreparer(clm)
    return {
        ASSESSMENT_3: AssessmentPreparer(clm),
        ASSESSMENT_4: ContractSnapshotPreparer(clm, assessment=True),
        SIGNATURE: ContractSnapshotPreparer(clm, assessment=False),
        ("crm.restore_assessment", "1.0.0"): RestorePreparer(),
        ("crm.restore_assessment", "2.0.0"): RestorePreparer(),
        ("crm.write_assessment", "2.0.0"): contract,  # retired; registered with its schema
        ("esign.send_for_signature", "2.0.0"): contract,
        ("email.send", "2.0.0"): EmailPreparer(directory),
    }


def _variables_for(clm: MockCLM) -> Callable[[RunRecord | None], Mapping[str, Any]]:
    def variables(run: RunRecord | None) -> Mapping[str, Any]:
        findings = run.findings if run is not None else None
        return {**TENANT_VARIABLES, "review": review_variables(findings, clm.deal_values())}

    return variables


def rating_history(
    entries: Sequence[HistoryEntry], args: Mapping[str, Any], run: RunRecord
) -> dict[str, list[str]]:
    """`lower_ratings`: the levels below the contract's standing rating, so a rule can ask
    whether this rating lowers it (high > medium > low). Empty with no earlier rating.

    The standing rating is the highest of the last rating that took effect and every counted
    rating after it, which may still take effect (waiting, running, unknown). Taking only the
    latest would let a waiting "medium" after a "high" make a second "medium" look like no change,
    and if legal then rejected the first, the contract would drop from high with no person. A
    level the tool no longer accepts is ignored."""
    took_effect = {StepStatus.SUCCEEDED, StepStatus.DONE_BY_PERSON}
    start = max((i for i, e in enumerate(entries) if e.status in took_effect), default=0)
    levels = [e.resolved_args.get("risk_level") for e in entries[start:]]
    known = [RATINGS.index(level) for level in levels if level in RATINGS]
    return {"lower_ratings": list(RATINGS[: max(known)]) if known else []}


RATING_HISTORY = HistorySubject(
    argument="contract_id",
    variables=rating_history,
    shown=("risk_level",),
    tools=(ASSESSMENT, "crm.restore_assessment"),
)


def review_variables(
    findings: Mapping[str, Any] | None, deal_values: Mapping[str, float]
) -> dict[str, list[str]]:
    """Values the downgrade rules compare a CRM assessment with.

    - `unsupported_levels`: ["low"] when the Reader's findings hold a high-severity flagged clause
      (text aimed at an AI reviewer is reported as one), so "low" contradicts them; else [].
      Catches a Reader fooled partway, not one that reports "low" with nothing flagged.
    - `large_deals`: contract ids whose deal value in the contract system is over the threshold.
      Catches a fully fooled "low" on a large deal. Since M17 every "low" also needs legal
      (provisional), so these two lists now only choose the reason the approver sees.
    """
    clauses = (findings or {}).get("flagged_clauses") or []
    contradicted = any(isinstance(c, Mapping) and c.get("severity") == "high" for c in clauses)
    return {
        "unsupported_levels": ["low"] if contradicted else [],
        "large_deals": sorted(
            cid for cid, value in deal_values.items() if value > LARGE_DEAL_THRESHOLD
        ),
    }


def _captured_contract(args: Mapping[str, Any]) -> RecordSnapshot:
    """Validate trusted preparation evidence before policy use or undo persistence."""
    from gawe.apps.record_binding import CONTRACT_FIELDS
    from gawe.core.preparation import PreparationError

    try:
        (raw,) = args["snapshots"]
        snapshot = RecordSnapshot.model_validate(raw)
        CONTRACT_FIELDS.validate_fields(snapshot)
        if (
            snapshot.resource != CONTRACT_FIELDS.resource
            or snapshot.subject != args["contract_id"]
            or snapshot.revision != args["record"]["contract_version"]
            or snapshot.basis != "preparation"
        ):
            raise ValueError("snapshot binding changed")
    except Exception as exc:
        raise PreparationError("record_version_unavailable") from exc
    return snapshot


def captured_variables(run: RunRecord, args: Mapping[str, Any]) -> Mapping[str, Any]:
    """Authorization for new versions never reads a different deal value after preparation."""
    snapshot = _captured_contract(args)
    value = snapshot.fields[0].value
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return {**TENANT_VARIABLES, "review": review_variables(run.findings, {snapshot.subject: value})}
