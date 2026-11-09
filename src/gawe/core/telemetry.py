"""Tracing and evaluation metrics behind a small interface.

Attributes are restricted to scalars (ids, counts, status names, booleans). Prompts, documents,
tool arguments and tool outputs are never passed to telemetry, so a misconfigured exporter
cannot leak them.

Metric names used by the engine:
    gawe.run.outcome            count, label: status
    gawe.run.duration_seconds   histogram
    gawe.plan.attempts          count, labels: valid, first_try
    gawe.step.outcome           count, labels: tool, status
    gawe.policy.decision        count, labels: tool, outcome
    gawe.verification           count, label: ok
    gawe.approval.latency_seconds  histogram, label: decision
    gawe.anchor.written         count, label: sink
    gawe.anchor.failures        count, label: sink
"""

import importlib
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

Attr = str | int | float | bool


class Span(Protocol):
    def set(self, key: str, value: Attr) -> None: ...


class Telemetry(Protocol):
    def span(self, name: str, **attributes: Attr) -> AbstractContextManager[Span]: ...

    def count(self, name: str, value: int = 1, **attributes: Attr) -> None: ...

    def observe(self, name: str, value: float, **attributes: Attr) -> None: ...


class _NoopSpan:
    def set(self, key: str, value: Attr) -> None:
        return None


class NoopTelemetry:
    @contextmanager
    def span(self, name: str, **attributes: Attr) -> Iterator[Span]:
        yield _NoopSpan()

    def count(self, name: str, value: int = 1, **attributes: Attr) -> None:
        return None

    def observe(self, name: str, value: float, **attributes: Attr) -> None:
        return None


@dataclass
class RecordedSpan:
    name: str
    attributes: dict[str, Attr]
    error: str | None = None

    def set(self, key: str, value: Attr) -> None:
        self.attributes[key] = value


@dataclass
class InMemoryTelemetry:
    """Keeps everything it is given; for tests and local inspection."""

    spans: list[RecordedSpan] = field(default_factory=list)
    counts: list[tuple[str, int, dict[str, Attr]]] = field(default_factory=list)
    observations: list[tuple[str, float, dict[str, Attr]]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @contextmanager
    def span(self, name: str, **attributes: Attr) -> Iterator[Span]:
        recorded = RecordedSpan(name, dict(attributes))
        with self._lock:
            self.spans.append(recorded)
        try:
            yield recorded
        except BaseException as exc:
            recorded.error = type(exc).__name__
            raise

    def count(self, name: str, value: int = 1, **attributes: Attr) -> None:
        with self._lock:
            self.counts.append((name, value, dict(attributes)))

    def observe(self, name: str, value: float, **attributes: Attr) -> None:
        with self._lock:
            self.observations.append((name, value, dict(attributes)))

    def observed(self, name: str) -> list[float]:
        with self._lock:
            return [v for n, v, _ in self.observations if n == name]

    def total(self, name: str, **where: Attr) -> int:
        return sum(
            v
            for n, v, attrs in self.counts
            if n == name and all(attrs.get(k) == w for k, w in where.items())
        )


class OtelTelemetry:
    """OpenTelemetry adapter. Needs `pip install "gawe[otel]"`; the SDK/exporter setup (endpoint,
    resource, sampling) is the application's job and is configured outside this class."""

    def __init__(self, *, tracer: Any = None, meter: Any = None) -> None:
        if tracer is None or meter is None:
            try:
                trace = importlib.import_module("opentelemetry.trace")
                metrics = importlib.import_module("opentelemetry.metrics")
            except ImportError as exc:
                raise RuntimeError(
                    'opentelemetry is not installed; run: pip install "gawe[otel]"'
                ) from exc
            tracer = tracer or trace.get_tracer("gawe")
            meter = meter or metrics.get_meter("gawe")
        self._tracer = tracer
        self._meter = meter
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._lock = threading.Lock()

    @contextmanager
    def span(self, name: str, **attributes: Attr) -> Iterator[Span]:
        with self._tracer.start_as_current_span(name, attributes=attributes) as otel_span:
            yield _OtelSpan(otel_span)

    def count(self, name: str, value: int = 1, **attributes: Attr) -> None:
        with self._lock:
            counter = self._counters.get(name)
            if counter is None:
                counter = self._counters[name] = self._meter.create_counter(name)
        counter.add(value, attributes)

    def observe(self, name: str, value: float, **attributes: Attr) -> None:
        with self._lock:
            histogram = self._histograms.get(name)
            if histogram is None:
                histogram = self._histograms[name] = self._meter.create_histogram(name)
        histogram.record(value, attributes)


class _OtelSpan:
    def __init__(self, span: Any) -> None:
        self._span = span

    def set(self, key: str, value: Attr) -> None:
        self._span.set_attribute(key, value)
