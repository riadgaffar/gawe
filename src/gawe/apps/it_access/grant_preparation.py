"""Trusted preparation for `iam.grant_access@2.0.0`: the account revision and the end date.

Both come from the system and the tenant's configuration, never from the model, and both go into
the arguments before policy and hashing. Every new grant expires; one lifetime for all grants
keeps preparation independent of the policy outcome, which is decided after it.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from gawe.connectors.base import ConnectorError
from gawe.connectors.mock_iam import MockIAM
from gawe.core.preparation import PreparationError, PreparedAction

MIN_LIFETIME_HOURS = 1
MAX_LIFETIME_HOURS = 30 * 24
DEFAULT_LIFETIME_HOURS = 7 * 24


def validate_lifetime(hours: object) -> int:
    if (
        not isinstance(hours, int)
        or isinstance(hours, bool)
        or not MIN_LIFETIME_HOURS <= hours <= MAX_LIFETIME_HOURS
    ):
        raise ValueError(
            f"grant lifetime must be {MIN_LIFETIME_HOURS}-{MAX_LIFETIME_HOURS} whole hours"
        )
    return hours


def format_expiry(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("grant end dates are computed in UTC")
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class GrantPreparer:
    def __init__(self, iam: MockIAM, *, lifetime_hours: int, clock: Callable[[], datetime]) -> None:
        self.iam = iam
        self.lifetime_hours = validate_lifetime(lifetime_hours)  # startup fails otherwise
        self.clock = clock

    def __call__(self, args: Mapping[str, Any], *, reapproval: bool) -> PreparedAction:
        try:
            revision = self.iam.user_version(args["email"])
        except ConnectorError as exc:
            raise PreparationError(exc.code) from exc
        except Exception as exc:  # noqa: BLE001 - any unreadable record fails closed
            raise PreparationError("record_version_unavailable") from exc
        if not isinstance(revision, str) or not revision:
            raise PreparationError("record_version_unavailable")
        expires_at = format_expiry(self.clock() + timedelta(hours=self.lifetime_hours))
        return PreparedAction(
            args={
                **args,
                "record": {"user_version": revision},
                "expires_at": expires_at,
                "lifetime_hours": self.lifetime_hours,
            },
            metadata={},
        )
