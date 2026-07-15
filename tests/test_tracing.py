"""T6 check: Langfuse OTLP config derivation + subprocess env inheritance."""
import base64

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
