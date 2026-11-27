"""Trusted preparation for the contract writes: bind the contract's current revision.

The revision is read from the contract system, never from the model. It goes into the arguments
before policy and hashing, so the approver approves, and the write later requires, that exact
revision of the contract.
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from jsonschema import Draft202012Validator

from gawe.apps.contract_review.tools import NO_RATING, PRIOR_SCHEMA
from gawe.apps.record_binding import CONTRACT_FIELDS, capture, utc_now
from gawe.connectors.base import ConnectorError
from gawe.connectors.mock_clm import MockCLM
from gawe.core.preparation import PreparationError, PreparedAction
from gawe.core.runs import RunRecord


class ContractRecordPreparer:
    def __init__(self, clm: MockCLM) -> None:
        self.clm = clm

    def __call__(
        self, args: Mapping[str, Any], *, reapproval: bool, run: RunRecord | None = None
    ) -> PreparedAction:
        try:
            revision = self.clm.contract_version(args["contract_id"])
        except ConnectorError as exc:
            raise PreparationError(exc.code) from exc
        except Exception as exc:  # noqa: BLE001 - any unreadable record fails closed
            raise PreparationError("record_version_unavailable") from exc
        if not isinstance(revision, str) or not revision:
            raise PreparationError("record_version_unavailable")
        return PreparedAction(args={**args, "record": {"contract_version": revision}}, metadata={})


class AssessmentPreparer:
    """crm.write_assessment@3.0.0 (M26): bind the revision and the rating it replaces.

    Both come from one read, and the write lands only while the contract still has that
    revision, so `prior` is exactly what the write replaces. A proposal can't supply either: the
    proposal schema has neither field, and preparation overwrites them."""

    def __init__(self, clm: MockCLM) -> None:
        self.clm = clm

    def __call__(
        self, args: Mapping[str, Any], *, reapproval: bool, run: RunRecord | None = None
    ) -> PreparedAction:
        try:
            current, revision = self.clm.current_assessment(args["contract_id"])
        except ConnectorError as exc:
            raise PreparationError(exc.code) from exc
        except Exception as exc:  # noqa: BLE001 - any unreadable record fails closed
            raise PreparationError("record_version_unavailable") from exc
        if not isinstance(revision, str) or not revision:
            raise PreparationError("record_version_unavailable")
        prior = {"risk_level": NO_RATING, "summary": ""} if current is None else current
        if not Draft202012Validator(PRIOR_SCHEMA).is_valid(prior):
            raise PreparationError("record_version_unavailable")
        return PreparedAction(
            args={
                **args,
                "record": {"contract_version": revision},
                "prior": {"risk_level": prior["risk_level"], "summary": prior["summary"]},
            },
            metadata={},
        )


class RestorePreparer:
    """The undo service already bound these values from the armed record, before planning."""

    def __call__(
        self, args: Mapping[str, Any], *, reapproval: bool, run: RunRecord | None = None
    ) -> PreparedAction:
        if run is None or run.profile != "undo":
            raise PreparationError("undo_unavailable")
        return PreparedAction(args=dict(args), metadata={})


class ContractSnapshotPreparer:
    """M27: deal value, prior rating and revision come from one trusted record read."""

    def __init__(
        self, clm: MockCLM, *, assessment: bool, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self.clm, self.assessment, self.clock = clm, assessment, clock

    def __call__(
        self, args: Mapping[str, Any], *, reapproval: bool, run: RunRecord | None = None
    ) -> PreparedAction:
        try:
            record, revision = self.clm.current_contract(args["contract_id"])
            snapshot = capture(CONTRACT_FIELDS, args["contract_id"], record, revision, self.clock)
            extra: dict[str, Any] = {}
            if self.assessment:
                prior = (
                    {"risk_level": record["risk_level"], "summary": record["risk_summary"]}
                    if "risk_level" in record
                    else {"risk_level": NO_RATING, "summary": ""}
                )
                if not Draft202012Validator(PRIOR_SCHEMA).is_valid(prior):
                    raise ValueError("invalid prior")
                extra["prior"] = prior
        except ConnectorError as exc:
            raise PreparationError(exc.code) from exc
        except Exception as exc:
            raise PreparationError("record_version_unavailable") from exc
        # The target still checks any proposed signature amount at the guarded write.
        return PreparedAction(
            args={
                **args,
                **extra,
                "record": {"contract_version": revision},
                "snapshots": [snapshot],
            },
            metadata={},
        )
