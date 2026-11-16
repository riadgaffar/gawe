"""Next-day review: what it records, how the sample is drawn, and where items are kept.

Each tenant gets one `daily_review` job per UTC day, due at 01:00 UTC the next day. The review
re-checks the day's policy decisions against the audit trail and the current rules, and draws a
keyed sample of automatic low ratings for a person to look at. Items are what a reviewer later
marks `confirmed` or `missed_risk`; those verdicts are what will measure the miss rate.

The sample is `HMAC-SHA256(review key, action hash)` read as a number, selected when below
rate x 2^256. The key is secret, so wording cannot be tuned to stay out of the sample; anyone
holding it can reproduce the draw. Items record the key's id, never the key.
"""

import hashlib
import hmac
import os
import threading
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field

from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.maintenance import MaintenanceJob
from gawe.core.models import FrozenModel
from gawe.core.policy.signing import KEY_ENV_VAR as SIGNING_KEY_ENV

KIND = "daily_review"
DUE_TIME = time(1, 0, tzinfo=UTC)
DEFAULT_RATE = 0.05
# Each tick schedules this many past days, so a scheduler that was down catches up. Jobs are
# idempotent by tenant and day, so days already scheduled add nothing.
CATCH_UP_DAYS = 7
KEY_ENV = "GAWE_REVIEW_SAMPLE_KEY"
MIN_KEY_BYTES = 32

Reason = Literal[
    "audit_mismatch",  # a stored decision and the audit trail disagree
    "superseded",  # an audited decision was replaced by a recipient refresh
    "signature_invalid",  # the stored decision's signature does not verify
    "action_mismatch",  # the stored arguments no longer hash to the decided action
    "rules_changed",  # the current rules decide differently, and the rule set changed
    "rules_may_have_changed",  # as above, for a decision from before rule-set digests
    "inputs_changed",  # same rules, different outcome: a value they read has changed
    "sampled_low",  # drawn for a person to check
]
Status = Literal["open", "confirmed", "missed_risk"]
Verdict = Literal["confirmed", "missed_risk"]


class ReviewError(Exception):
    pass


class ReviewItemNotFoundError(ReviewError):
    pass


class ReviewAlreadyDecidedError(ReviewError):
    pass


class ReviewItem(FrozenModel):
    item_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    tenant_id: str
    day: date
    run_id: str
    step_id: str | None = None
    action_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: Reason
    key_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    status: Status = "open"
    created_at: AwareDatetime
    event_seq: int | None = Field(default=None, ge=1)
    decided_by: str | None = None
    decided_at: AwareDatetime | None = None


def item_identity(
    tenant_id: str,
    day: date,
    run_id: str,
    step_id: str | None,
    action_hash: str | None,
    reason: str,
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "domain": "gawe.review_item.v1",
                "tenant_id": tenant_id,
                "day": day.isoformat(),
                "run_id": run_id,
                "step_id": step_id,
                "action_hash": action_hash,
                "reason": reason,
            }
        )
    )


def day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0, tzinfo=UTC))
    return start, start + timedelta(days=1)


def review_job(tenant_id: str, day: date) -> MaintenanceJob:
    """The tenant's review of `day`. Every field is fixed by the tenant and the day (creation is
    stamped at the day's end), so any scheduler, at any time, adds the same job once."""
    due = datetime.combine(day + timedelta(days=1), DUE_TIME)
    return MaintenanceJob(
        job_id=sha256_hex(
            canonical_json(
                {"domain": "gawe.daily_review.v1", "tenant_id": tenant_id, "day": day.isoformat()}
            )
        ),
        kind=KIND,
        tenant_id=tenant_id,
        run_id=None,
        plan_version=None,
        step_id=None,
        action_hash=None,
        operation_key=f"daily_review:{day.isoformat()}",
        created_at=day_window(day)[1],
        deadline=due + timedelta(days=1),
        due_at=due,
        payload={"day": day.isoformat()},
    )


# --- the sample ---------------------------------------------------------------------------------


class ReviewKey:
    """The sample key. Separate from the policy signing key; only its id is ever recorded."""

    def __init__(self, key: bytes, *, signing_key: bytes | None = None) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"the review sample key must be at least {MIN_KEY_BYTES} bytes")
        if signing_key is not None and hmac.compare_digest(key, signing_key):
            raise ValueError("the review sample key must differ from the policy signing key")
        self._key = key
        self.key_id = hashlib.sha256(b"gawe.review_key.v1\n" + key).hexdigest()[:16]

    @classmethod
    def from_env(cls) -> "ReviewKey | None":
        """The key from GAWE_REVIEW_SAMPLE_KEY, or None when unset (the sample is then off).
        Refuses a short key, a non-hex key and the policy signing key."""
        raw = os.environ.get(KEY_ENV, "").strip()
        if not raw:
            return None
        try:
            key = bytes.fromhex(raw)
        except ValueError as exc:
            raise ValueError(f"{KEY_ENV} must be hex-encoded") from exc
        signing = os.environ.get(SIGNING_KEY_ENV, "").strip()
        try:
            signing_key = bytes.fromhex(signing) if signing else None
        except ValueError:
            signing_key = None  # the signer reports its own malformed key
        return cls(key, signing_key=signing_key)

    def draw(self, action_hash: str) -> int:
        digest = hmac.new(self._key, action_hash.encode("ascii"), hashlib.sha256).digest()
        return int.from_bytes(digest, "big")

    def selects(self, action_hash: str, rate: float = DEFAULT_RATE) -> bool:
        if not 0 <= rate <= 1:
            raise ValueError("sample rate must be between 0 and 1")
        return self.draw(action_hash) < int(rate * 2**256)


# --- storage ------------------------------------------------------------------------------------


class ReviewStore(Protocol):
    def add(self, item: ReviewItem) -> ReviewItem:
        """Insert once; return the stored item (the existing one if already there)."""
        ...

    def get(self, tenant_id: str, item_id: str) -> ReviewItem: ...

    def list(
        self, tenant_id: str, *, status: Status | None = None, limit: int = 200
    ) -> list[ReviewItem]: ...

    def set_event(self, tenant_id: str, item_id: str, seq: int) -> ReviewItem: ...

    def decide(
        self,
        tenant_id: str,
        item_id: str,
        *,
        verdict: Verdict,
        decided_by: str,
        decided_at: datetime,
    ) -> ReviewItem:
        """Record a verdict once (ReviewAlreadyDecidedError otherwise)."""
        ...


class InMemoryReviewStore:
    def __init__(self) -> None:
        self._items: dict[str, ReviewItem] = {}
        self._lock = threading.Lock()

    def add(self, item: ReviewItem) -> ReviewItem:
        with self._lock:
            return self._items.setdefault(item.item_id, item)

    def get(self, tenant_id: str, item_id: str) -> ReviewItem:
        with self._lock:
            item = self._items.get(item_id)
        if item is None or item.tenant_id != tenant_id:
            raise ReviewItemNotFoundError(f"no review item {item_id}")
        return item

    def list(
        self, tenant_id: str, *, status: Status | None = None, limit: int = 200
    ) -> list[ReviewItem]:
        with self._lock:
            items = [
                i
                for i in self._items.values()
                if i.tenant_id == tenant_id and (status is None or i.status == status)
            ]
        items.sort(key=lambda i: (i.day, i.created_at, i.item_id))
        return items[: max(0, limit)]

    def set_event(self, tenant_id: str, item_id: str, seq: int) -> ReviewItem:
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.tenant_id != tenant_id:
                raise ReviewItemNotFoundError(f"no review item {item_id}")
            if item.event_seq is None:
                item = item.model_copy(update={"event_seq": seq})
                self._items[item_id] = item
            return item

    def decide(
        self,
        tenant_id: str,
        item_id: str,
        *,
        verdict: Verdict,
        decided_by: str,
        decided_at: datetime,
    ) -> ReviewItem:
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.tenant_id != tenant_id:
                raise ReviewItemNotFoundError(f"no review item {item_id}")
            if item.status != "open":
                raise ReviewAlreadyDecidedError(f"review item {item_id} was already decided")
            item = item.model_copy(
                update={"status": verdict, "decided_by": decided_by, "decided_at": decided_at}
            )
            self._items[item_id] = item
            return item
