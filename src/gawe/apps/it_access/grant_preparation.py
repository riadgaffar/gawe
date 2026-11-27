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
from gawe.connectors.mock_tickets import MockTickets
from gawe.core.preparation import PreparationError, PreparedAction
from gawe.core.runs import RunRecord
from gawe.core.sources import SourceError

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
    def __init__(
        self,
        iam: MockIAM,
        *,
        lifetime_hours: int,
        clock: Callable[[], datetime],
        tickets: MockTickets | None = None,
        snapshots: bool = False,
    ) -> None:
        self.iam = iam
        self.snapshots = snapshots
        self.lifetime_hours = validate_lifetime(lifetime_hours)  # startup fails otherwise
        self.clock = clock
        # With a ticket system, every grant needs its run's ticket, still open (M16c D5).
        self.tickets = tickets

    def __call__(
        self, args: Mapping[str, Any], *, reapproval: bool, run: RunRecord | None = None
    ) -> PreparedAction:
        if self.tickets is not None:
            self._require_open_ticket(run)
        try:
            revision = self.iam.user_version(args["email"])
        except ConnectorError as exc:
            raise PreparationError(exc.code) from exc
        except Exception as exc:  # noqa: BLE001 - any unreadable record fails closed
            raise PreparationError("record_version_unavailable") from exc
        if not isinstance(revision, str) or not revision:
            raise PreparationError("record_version_unavailable")
        now = self.clock()
        expires_at = format_expiry(now + timedelta(hours=self.lifetime_hours))
        extra: dict[str, Any] = {}
        if self.snapshots:
            from gawe.apps.record_binding import ACCOUNT_FIELDS, capture

            try:
                extra["snapshots"] = [
                    capture(
                        ACCOUNT_FIELDS, args["email"].strip().lower(), {}, revision, lambda: now
                    )
                ]
            except Exception as exc:
                raise PreparationError("record_version_unavailable") from exc
        return PreparedAction(
            args={
                **args,
                **extra,
                "record": {"user_version": revision},
                "expires_at": expires_at,
                "lifetime_hours": self.lifetime_hours,
            },
            metadata={},
        )

    def _require_open_ticket(self, run: RunRecord | None) -> None:
        assert self.tickets is not None
        if run is None or run.source_id is None:
            raise PreparationError("ticket_required")
        try:
            ticket = self.tickets.get(run.tenant_id, run.source_id)
        except SourceError as exc:
            raise PreparationError(exc.code) from exc
        except Exception as exc:  # noqa: BLE001 - an unreadable ticket fails closed
            raise PreparationError("source_unreadable") from exc
        if ticket.status != "open":
            raise PreparationError("ticket_not_open")
