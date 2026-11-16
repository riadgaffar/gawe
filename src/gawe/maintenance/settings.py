"""Validated schedule and deadline settings for the maintenance runner.

Defaults: a tick every 30 seconds, a five-minute verification deadline from the persisted
dispatch intent. Supported bounds are deliberately narrow so a typo cannot disable resolution
(an interval longer than the deadline would turn every delayed read into `unknown`).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_DEADLINE_SECONDS = 300
INTERVAL_BOUNDS = (1, 300)
DEADLINE_BOUNDS = (30, 3600)
BATCH_BOUNDS = (1, 500)
MAX_JOBS_BOUNDS = (1, 10_000)

INTERVAL_ENV = "GAWE_MAINTENANCE_INTERVAL_SECONDS"
DEADLINE_ENV = "GAWE_VERIFICATION_DEADLINE_SECONDS"
BATCH_ENV = "GAWE_MAINTENANCE_BATCH_SIZE"
MAX_JOBS_ENV = "GAWE_MAINTENANCE_MAX_JOBS"


class SettingsError(ValueError):
    """A maintenance setting is outside its supported bounds."""


def _check(name: str, value: int, bounds: tuple[int, int]) -> int:
    low, high = bounds
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SettingsError(f"{name} must be an integer from {low} to {high}")
    return value


@dataclass(frozen=True)
class MaintenanceSettings:
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS
    batch_size: int = 50  # jobs claimed per page
    max_jobs: int = 500  # jobs processed per tick, across pages

    def __post_init__(self) -> None:
        _check(INTERVAL_ENV, self.interval_seconds, INTERVAL_BOUNDS)
        _check(DEADLINE_ENV, self.deadline_seconds, DEADLINE_BOUNDS)
        _check(BATCH_ENV, self.batch_size, BATCH_BOUNDS)
        _check(MAX_JOBS_ENV, self.max_jobs, MAX_JOBS_BOUNDS)
        if self.interval_seconds >= self.deadline_seconds:
            raise SettingsError(f"{INTERVAL_ENV} must be shorter than {DEADLINE_ENV}")

    @property
    def interval(self) -> timedelta:
        return timedelta(seconds=self.interval_seconds)

    @property
    def deadline(self) -> timedelta:
        return timedelta(seconds=self.deadline_seconds)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "MaintenanceSettings":
        env = os.environ if environ is None else environ

        def read(name: str, default: int) -> int:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise SettingsError(f"{name} must be an integer") from exc

        return cls(
            interval_seconds=read(INTERVAL_ENV, DEFAULT_INTERVAL_SECONDS),
            deadline_seconds=read(DEADLINE_ENV, DEFAULT_DEADLINE_SECONDS),
            batch_size=read(BATCH_ENV, 50),
            max_jobs=read(MAX_JOBS_ENV, 500),
        )
