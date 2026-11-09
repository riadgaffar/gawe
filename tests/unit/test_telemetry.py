from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from gawe.core.telemetry import InMemoryTelemetry, NoopTelemetry, OtelTelemetry


def test_noop_accepts_everything() -> None:
    t = NoopTelemetry()
    with t.span("x", a=1) as s:
        s.set("k", "v")
    t.count("c")
    t.observe("o", 1.0)


def test_in_memory_records_spans_counts_and_observations() -> None:
    t = InMemoryTelemetry()
    with t.span("work", run_id="r") as s:
        s.set("status", "ok")
    t.count("c", ok=True)
    t.count("c", 2, ok=True)
    t.count("c", ok=False)
    t.observe("o", 1.5)
    assert t.spans[0].attributes == {"run_id": "r", "status": "ok"}
    assert t.total("c", ok=True) == 3 and t.total("c") == 4
    assert t.observed("o") == [1.5]


def test_in_memory_span_records_error_and_reraises() -> None:
    t = InMemoryTelemetry()
    with pytest.raises(RuntimeError), t.span("boom"):
        raise RuntimeError("x")
    assert t.spans[0].error == "RuntimeError"


class FakeOtelSpan:
    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[tuple[str, Mapping[str, Any], FakeOtelSpan]] = []

    @contextmanager
    def start_as_current_span(self, name: str, attributes: Mapping[str, Any]) -> Any:
        span = FakeOtelSpan()
        self.spans.append((name, attributes, span))
        yield span


class FakeInstrument:
    def __init__(self) -> None:
        self.calls: list[tuple[float, Mapping[str, Any]]] = []

    def add(self, value: float, attributes: Mapping[str, Any]) -> None:
        self.calls.append((value, attributes))

    record = add


class FakeMeter:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.instruments: dict[str, FakeInstrument] = {}

    def _make(self, name: str) -> FakeInstrument:
        self.created.append(name)
        return self.instruments.setdefault(name, FakeInstrument())

    create_counter = _make
    create_histogram = _make


def test_otel_adapter_maps_calls_and_reuses_instruments() -> None:
    tracer, meter = FakeTracer(), FakeMeter()
    t = OtelTelemetry(tracer=tracer, meter=meter)
    with t.span("s", run_id="r") as span:
        span.set("status", "ok")
    t.count("c", 2, ok=True)
    t.count("c", ok=False)
    t.observe("o", 3.0, d="x")
    assert tracer.spans[0][0] == "s" and tracer.spans[0][2].attrs == {"status": "ok"}
    assert meter.created == ["c", "o"]
    assert meter.instruments["c"].calls == [(2, {"ok": True}), (1, {"ok": False})]
    assert meter.instruments["o"].calls == [(3.0, {"d": "x"})]


def test_otel_without_sdk_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    real = importlib.import_module

    def fake(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith("opentelemetry"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake)
    with pytest.raises(RuntimeError, match="gawe\\[otel\\]"):
        OtelTelemetry()
