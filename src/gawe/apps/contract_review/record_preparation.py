"""Trusted preparation for the contract writes: bind the contract's current revision.

The revision is read from the contract system, never from the model. It goes into the arguments
before policy and hashing, so the approver approves, and the write later requires, that exact
revision of the contract.
"""

from collections.abc import Mapping
from typing import Any

from gawe.connectors.base import ConnectorError
from gawe.connectors.mock_clm import MockCLM
from gawe.core.preparation import PreparationError, PreparedAction


class ContractRecordPreparer:
    def __init__(self, clm: MockCLM) -> None:
        self.clm = clm

    def __call__(self, args: Mapping[str, Any], *, reapproval: bool) -> PreparedAction:
        try:
            revision = self.clm.contract_version(args["contract_id"])
        except ConnectorError as exc:
            raise PreparationError(exc.code) from exc
        except Exception as exc:  # noqa: BLE001 - any unreadable record fails closed
            raise PreparationError("record_version_unavailable") from exc
        if not isinstance(revision, str) or not revision:
            raise PreparationError("record_version_unavailable")
        return PreparedAction(args={**args, "record": {"contract_version": revision}}, metadata={})
