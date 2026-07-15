"""T6 — OTel step-level spans + usage exported to self-hosted Langfuse.

Two jobs (arch §3.4, phase0-plan T6):

1. Config: derive Langfuse's OTLP endpoint + Basic-auth header from
   LANGFUSE_* env, install a Python TracerProvider that exports there, and
   re-publish the same coordinates as standard OTEL_* env vars so the
   `claude -p` subprocess (T5) inherits them and exports its own per-model /
   per-tool spans to the SAME Langfuse. Step-level spans are the GUARANTEED
   floor; per-call spans are the stretch that rides on CC's export.

2. Instrumentation: `step_span()`, wrapped by the poll loop around
   `agent.step()`.

ponytail: `configure_tracing()`'s live call site is T7's run_phase0.py (the
process entrypoint) — global tracing config belongs at startup, not in the
per-agent loop. Until T7 runs it live, step_span() is a cheap no-op and the
OTLP export path is unproven (same deferral as T5's `_run_claude`). Upgrade
path: T7 calls configure_tracing() once before starting run_agent.
"""
import base64
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = "agentic-os"

# No-op until configure_tracing() installs a real one. We deliberately DON'T
# call trace.set_tracer_provider() (global mutation) — tests set _tracer.
_tracer = trace.get_tracer(__name__)


def _langfuse_otlp_config(host: str, public_key: str, secret_key: str) -> tuple[str, dict]:
    """(base_endpoint, headers) for Langfuse's OTLP receiver.

    base_endpoint is the OTLP *base* — consumers that read
    OTEL_EXPORTER_OTLP_ENDPOINT append `/v1/traces` themselves.
    """
    base = host.rstrip("/") + "/api/public/otel"
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return base, {"Authorization": f"Basic {token}"}


def configure_tracing() -> bool:
    """Install the Langfuse OTLP exporter and publish OTEL_* for the subprocess.

    No-op returning False when LANGFUSE_* keys are absent (local dev / tests).
    """
    global _tracer
    host = os.environ.get("LANGFUSE_HOST")
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    if not (host and pk and sk):
        return False

    base, headers = _langfuse_otlp_config(host, pk, sk)

    # Publish to the environment so the T5 `claude -p` subprocess inherits the
    # same endpoint/creds (it reads OTEL_EXPORTER_OTLP_ENDPOINT to switch its
    # own telemetry on). base, not base+/v1/traces: those consumers suffix it.
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = base
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization={headers['Authorization']}"

    # Python-side exporter: the `endpoint` kwarg is the EXACT traces URL and is
    # not auto-suffixed, so pass base + /v1/traces. Headers passed as a dict
    # (robust — no round-trip through the env-string parser).
    provider = TracerProvider(resource=Resource.create({"service.name": _SERVICE_NAME}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=base + "/v1/traces", headers=headers))
    )
    _tracer = provider.get_tracer(__name__)
    return True


def usage_attrs(usage: dict) -> dict:
    """Usage dict -> span attributes (primitives only, `usage.`-prefixed).

    OTel attribute values must be primitives (or homogeneous sequences), so
    nested dicts / None are dropped rather than crashing the span.
    """
    return {
        f"usage.{k}": v
        for k, v in usage.items()
        if isinstance(v, (int, float, str, bool))
    }


@contextmanager
def step_span(agent_name: str, run_id: str, step_n: int, saw: list):
    """Span around one agent.step(); yields the span for post-hoc usage attrs."""
    with _tracer.start_as_current_span("agent.step") as span:
        span.set_attribute("agent", agent_name)
        span.set_attribute("run_id", run_id)
        span.set_attribute("step_n", step_n)
        span.set_attribute("saw_events", saw)
        yield span
