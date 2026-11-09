import pytest

from gawe.api.throttle import FailureThrottle


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(
    limit: int = 3, window: float = 60.0, max_keys: int = 100
) -> tuple[FailureThrottle, Clock]:
    clock = Clock()
    return FailureThrottle(
        limit=limit, window_seconds=window, max_keys=max_keys, clock=clock
    ), clock


def test_under_the_limit_is_never_blocked() -> None:
    t, _ = make()
    for _ in range(2):
        t.record_failure("a")
    assert t.retry_after("a") is None


def test_reaching_the_limit_blocks_and_says_how_long() -> None:
    t, clock = make()
    for _ in range(3):
        t.record_failure("a")
    clock.now += 20
    assert t.retry_after("a") == 41  # the oldest failure leaves the window in 40s, rounded up


def test_the_block_ends_when_the_window_passes() -> None:
    t, clock = make()
    for _ in range(3):
        t.record_failure("a")
    clock.now += 61
    assert t.retry_after("a") is None


def test_each_address_is_counted_separately() -> None:
    t, _ = make()
    for _ in range(3):
        t.record_failure("a")
    assert t.retry_after("a") is not None
    assert t.retry_after("b") is None


def test_old_failures_age_out_one_by_one() -> None:
    t, clock = make()
    t.record_failure("a")
    clock.now += 30
    t.record_failure("a")
    t.record_failure("a")
    assert t.retry_after("a") is not None
    clock.now += 31  # the first failure is now outside the window; two remain
    assert t.retry_after("a") is None


def test_memory_is_bounded() -> None:
    t, _ = make(limit=1, max_keys=3)
    for i in range(10):
        t.record_failure(f"addr-{i}")
    assert t.retry_after("addr-0") is None  # forgotten
    assert t.retry_after("addr-9") is not None  # recent ones kept


@pytest.mark.parametrize("kw", [{"limit": 0}, {"window_seconds": 0}, {"max_keys": 0}])
def test_invalid_settings_are_refused(kw: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        FailureThrottle(**kw)  # type: ignore[arg-type]
