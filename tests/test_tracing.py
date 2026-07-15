"""T6 check: Langfuse OTLP config derivation + subprocess env inheritance."""
import base64
import json

from observability import tracing


def test_langfuse_otlp_config_derives_base_endpoint_and_basic_auth():
    endpoint, headers = tracing._langfuse_otlp_config(
        "http://langfuse-web:3000", "pk-lf-abc", "sk-lf-xyz"
    )
    assert endpoint == "http://langfuse-web:3000/api/public/otel"
    expected = "Basic " + base64.b64encode(b"pk-lf-abc:sk-lf-xyz").decode()
    assert headers == {"Authorization": expected}


def test_langfuse_otlp_config_strips_trailing_slash():
    endpoint, _ = tracing._langfuse_otlp_config("http://host:3000/", "pk", "sk")
    assert endpoint == "http://host:3000/api/public/otel"


def test_configure_tracing_noop_without_keys(monkeypatch):
    for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
              "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS"):
        monkeypatch.delenv(k, raising=False)
    assert tracing.configure_tracing() is False
    # No endpoint published -> T5 subprocess leaves telemetry off.
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in tracing.os.environ


def test_configure_tracing_publishes_otel_env_for_subprocess(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse-web:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-abc")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-xyz")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)

    assert tracing.configure_tracing() is True
    # Base endpoint (consumers append /v1/traces) so T5's env check fires.
    assert tracing.os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "http://langfuse-web:3000/api/public/otel"
    )
    b64 = base64.b64encode(b"pk-lf-abc:sk-lf-xyz").decode()
    assert tracing.os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == f"Authorization=Basic {b64}"


from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


def _capture_spans(monkeypatch):
    """Point tracing._tracer at an in-memory exporter; return the exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("test"))
    return exporter


def test_usage_attrs_keeps_primitives_and_prefixes():
    attrs = tracing.usage_attrs(
        {"input_tokens": 10, "total_cost_usd": 0.02, "model": "opus",
         "nested": {"x": 1}, "missing": None}
    )
    assert attrs == {
        "usage.input_tokens": 10,
        "usage.total_cost_usd": 0.02,
        "usage.model": "opus",
    }


def test_usage_attrs_on_empty():
    assert tracing.usage_attrs({}) == {}


def test_step_span_records_identity_window_and_usage(monkeypatch):
    exporter = _capture_spans(monkeypatch)
    with tracing.step_span("worker", "r1", 3, [10, 12]) as span:
        span.set_attributes(tracing.usage_attrs({"tokens": 7}))

    (rec,) = exporter.get_finished_spans()
    assert rec.name == "agent.step"
    assert rec.attributes["agent"] == "worker"
    assert rec.attributes["run_id"] == "r1"
    assert rec.attributes["step_n"] == 3
    assert list(rec.attributes["saw_events"]) == [10, 12]
    assert rec.attributes["usage.tokens"] == 7
    assert rec.attributes["langfuse.observation.input"] == "[]"  # no input -> empty array


def test_shutdown_tracing_noop_when_unconfigured(monkeypatch):
    # No provider stored -> must not raise.
    monkeypatch.setattr(tracing, "_provider", None)
    tracing.shutdown_tracing()  # no error == pass


def test_shutdown_tracing_flushes_provider(monkeypatch):
    class FakeProvider:
        def __init__(self):
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    fake = FakeProvider()
    monkeypatch.setattr(tracing, "_provider", fake)
    tracing.shutdown_tracing()
    assert fake.shutdown_called is True


def test_generation_attrs_maps_tokens_and_cost():
    # Langfuse-recognized keys so the Tokens/Cost columns populate.
    attrs = tracing.generation_attrs(
        {"input_tokens": 10, "output_tokens": 4, "total_cost_usd": 0.02,
         "cache_read_input_tokens": 3}
    )
    assert attrs["langfuse.observation.type"] == "generation"
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {
        "input": 10, "output": 4
    }
    assert json.loads(attrs["langfuse.observation.cost_details"]) == {"total": 0.02}


def test_generation_attrs_empty_without_usage():
    # No tokens and no cost -> nothing to mark as a generation.
    assert tracing.generation_attrs({}) == {}
    assert tracing.generation_attrs({"model": "opus"}) == {}


def test_step_span_sets_input_when_provided(monkeypatch):
    exporter = _capture_spans(monkeypatch)
    events = [{"id": 10, "type": "task.created", "payload": {"goal": "hi"}}]
    with tracing.step_span("worker", "r1", 1, [10, 10], input_events=events):
        pass

    (rec,) = exporter.get_finished_spans()
    assert json.loads(rec.attributes["langfuse.observation.input"]) == events
