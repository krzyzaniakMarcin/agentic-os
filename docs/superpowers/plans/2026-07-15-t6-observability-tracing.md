# T6 — Observability / Tracing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `observability/tracing.py` — the *guaranteed* step-level OTel span + usage export to self-hosted Langfuse, plus the `OTEL_*` env wiring so the `claude -p` subprocess (T5) exports per-call spans to the same endpoint.

**Architecture:** `tracing.py` has two responsibilities. (1) **Config:** derive Langfuse's OTLP endpoint + Basic-auth header from `LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY`, install a Python `TracerProvider` pointed there, and export the same coordinates as standard `OTEL_*` env vars so the T5 subprocess inherits them (T5 already reads `OTEL_EXPORTER_OTLP_ENDPOINT`). (2) **Instrumentation:** a `step_span()` context manager that the poll loop wraps around `agent.step()`, recording `agent`/`run_id`/`step_n`/`saw_events` and the returned `usage`. Step-level spans are the guaranteed floor; per-tool-call spans come from Claude Code's own OTel export when it's wired (T5) and verified live (T7) — the stretch.

**Tech Stack:** Python 3.12, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, pytest.

## Global Constraints

- **No global OTel mutation.** Do not call `opentelemetry.trace.set_tracer_provider()`. Store the configured tracer in a module-level `_tracer`; `step_span()` reads it. Default `_tracer` is the API's no-op tracer, so everything degrades to a cheap no-op until `configure_tracing()` runs. (This keeps tests isolatable — they set `tracing._tracer` directly.)
- **Graceful degradation.** With no `LANGFUSE_*` keys in the environment, `configure_tracing()` is a no-op returning `False` and sets no `OTEL_*` vars — local runs and unit tests must not require Langfuse.
- **Two endpoint forms, both correct.** The Langfuse OTLP *base* is `{LANGFUSE_HOST}/api/public/otel`. Consumers that read `OTEL_EXPORTER_OTLP_ENDPOINT` (the CC subprocess) append `/v1/traces` themselves, so the **env var gets the base**. The Python `OTLPSpanExporter(endpoint=...)` kwarg is the *exact* traces URL and is **not** auto-suffixed, so it gets **base + `/v1/traces`**.
- **Auth:** `Authorization: Basic base64("{PUBLIC_KEY}:{SECRET_KEY}")`. Pass headers as a dict to the Python exporter (robust); also format them as an `OTEL_EXPORTER_OTLP_HEADERS` string for the subprocess.
- Package must be added to `[tool.setuptools] packages` in `pyproject.toml` or it won't install.
- Follow the existing test style: plain `pytest` + `monkeypatch`, no new fixtures/frameworks (see `tests/test_poll_loop.py`).

---

### Task 1: Config layer — Langfuse OTLP endpoint/auth derivation + `configure_tracing()`

Derive the Langfuse OTLP coordinates, install a Python `TracerProvider` that exports there, and publish the same coordinates as `OTEL_*` env vars for the T5 subprocess to inherit.

**Files:**
- Modify: `pyproject.toml` (add two deps; add `observability` to packages)
- Create: `observability/__init__.py`
- Create: `observability/tracing.py`
- Test: `tests/test_tracing.py`

**Interfaces:**
- Consumes: environment variables `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`.
- Produces:
  - `_langfuse_otlp_config(host: str, public_key: str, secret_key: str) -> tuple[str, dict[str, str]]` — returns `(base_endpoint, headers)` where `base_endpoint = host.rstrip("/") + "/api/public/otel"` and `headers = {"Authorization": "Basic <b64>"}`.
  - `configure_tracing() -> bool` — reads env; if the three `LANGFUSE_*` are all present, sets `os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]` (base), `os.environ["OTEL_EXPORTER_OTLP_HEADERS"]` (`"Authorization=Basic <b64>"`), installs the module `_tracer` with an OTLP exporter at `base + "/v1/traces"`, and returns `True`. Returns `False` and touches nothing when keys are absent. Idempotent.
  - Module global `_tracer` (default: `opentelemetry.trace.get_tracer(__name__)` no-op tracer).

- [ ] **Step 1: Add dependencies and package registration**

Edit `pyproject.toml`. Add the two OTel packages to `dependencies` (they are core kernel deps — the traced runtime needs them at runtime, not just in dev):

```toml
dependencies = [
    "asyncpg>=0.29",
    "mcp>=1.2",
    "opentelemetry-sdk>=1.20",
    "opentelemetry-exporter-otlp-proto-http>=1.20",
]
```

Add `observability` to the packages list:

```toml
[tool.setuptools]
packages = ["substrate", "agent", "agent.runtimes", "observability"]
```

- [ ] **Step 2: Install the new deps**

Run: `uv sync --extra dev`
Expected: resolves and installs `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http` (and their transitive `opentelemetry-api`, `opentelemetry-exporter-otlp-proto-common`).

- [ ] **Step 3: Write the failing tests**

Create `tests/test_tracing.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tracing.py -v`
Expected: FAIL — `AttributeError: module 'observability.tracing' has no attribute '_langfuse_otlp_config'` (module/functions don't exist yet).

- [ ] **Step 5: Write the config implementation**

Create `observability/__init__.py`:

```python
```

(empty file — package marker)

Create `observability/tracing.py`:

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tracing.py -v`
Expected: PASS (4 tests).

- [ ] **Step 7: Run the full suite (nothing regressed)**

Run: `python3 -m pytest -q`
Expected: PASS — all prior tests still green (43 before this task) plus the 4 new ones.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml observability/__init__.py observability/tracing.py tests/test_tracing.py
git commit -m "feat(t6): Langfuse OTLP config + OTEL_* subprocess env in tracing.py"
```

---

### Task 2: Instrumentation — `step_span()` + `usage_attrs()` and poll-loop wiring

Wrap every `agent.step()` in an OTel span carrying identity, the saw-events window, and the returned usage. This is the guaranteed step-level visibility in Langfuse, independent of Claude Code's own export.

**Files:**
- Modify: `observability/tracing.py` (add `step_span`, `usage_attrs`)
- Modify: `agent/poll_loop.py:24-36` (wrap the step call)
- Test: `tests/test_tracing.py` (append span-capture tests)
- Test: `tests/test_poll_loop.py` (append one integration test)

**Interfaces:**
- Consumes: module `_tracer` from Task 1.
- Produces:
  - `usage_attrs(usage: dict) -> dict[str, int | float | str]` — flattens a usage dict to span attributes, keeping only primitive (`int`/`float`/`str`/`bool`) values, each prefixed `usage.` (OTel attributes must be primitives or sequences of one primitive type; drop nested/None values).
  - `step_span(agent_name: str, run_id: str, step_n: int, saw: list[int])` — a context manager (`@contextmanager`) that starts a current span named `agent.step` with attributes `agent`, `run_id`, `step_n`, `saw_events` (the `saw` list of two ints), and **yields the span** so the caller can attach usage after `step()` returns.

- [ ] **Step 1: Write the failing tests (span capture)**

Append to `tests/test_tracing.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tracing.py -k "usage_attrs or step_span" -v`
Expected: FAIL — `AttributeError: module 'observability.tracing' has no attribute 'usage_attrs'`.

- [ ] **Step 3: Implement `usage_attrs` and `step_span`**

Add `from contextlib import contextmanager` to the imports at the top of `observability/tracing.py`, then append these functions:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tracing.py -k "usage_attrs or step_span" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire the span into the poll loop**

Edit `agent/poll_loop.py`. Add the import near the top:

```python
from observability import tracing
```

Replace the step-invocation + record block (currently lines 24-36) so the step call and its `agent.step` record are wrapped in a span:

```python
        saw = [events[0]["id"], events[-1]["id"]]
        with tracing.step_span(agent.name, agent.run_id, agent.step_n + 1, saw) as span:
            emitted, usage = await agent.step(events)
            span.set_attributes(tracing.usage_attrs(usage))
        for e in emitted:  # inert for the CC runtime (emits=[]); real for others
            await log.emit(
                agent.name, e.type, e.payload, run_id=agent.run_id,
                reply_to=e.reply_to, correlation=e.correlation,
            )
        agent.step_n += 1
        await log.emit(
            agent.name, "agent.step",
            {"step_n": agent.step_n, "saw_events": saw, "usage": usage},
            run_id=agent.run_id,
        )
        cursor = events[-1]["id"]
```

(`step_n + 1` matches the `agent.step` record's `step_n`, which is emitted after the post-increment.)

- [ ] **Step 6: Write the failing poll-loop integration test**

Append to `tests/test_poll_loop.py` (add the imports at the top of the new test's needs — reuse the existing `_FakeAgent`):

```python
def _capture_step_spans(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from observability import tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("test"))
    return exporter


async def test_loop_emits_step_span_with_usage(monkeypatch):
    exporter = _capture_step_spans(monkeypatch)

    async def fake_read(**kw):
        return [{"id": 10}, {"id": 12}]

    async def fake_emit(*a, **k):
        return {"id": 1, "ts": 0.0}

    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "emit", fake_emit)

    a = _FakeAgent(Role(name="worker", subscribes_to=["task.created"]),
                   run_id="r1", emits=[])
    await poll_loop.run_agent(a)

    (rec,) = exporter.get_finished_spans()
    assert rec.name == "agent.step"
    assert rec.attributes["agent"] == "worker"
    assert rec.attributes["step_n"] == 1
    assert list(rec.attributes["saw_events"]) == [10, 12]
    assert rec.attributes["usage.tokens"] == 7  # _FakeAgent returns {"tokens": 7}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_poll_loop.py::test_loop_emits_step_span_with_usage tests/test_tracing.py -v`
Expected: PASS.

- [ ] **Step 8: Run the full suite (nothing regressed)**

Run: `python3 -m pytest -q`
Expected: PASS — existing poll-loop tests still green (`step_span` is a no-op there since `_tracer` isn't configured), plus all Task 1 + Task 2 tests.

- [ ] **Step 9: Commit**

```bash
git add observability/tracing.py agent/poll_loop.py tests/test_tracing.py tests/test_poll_loop.py
git commit -m "feat(t6): step-level OTel spans + usage wired into the poll loop"
```

---

## Deferred to T7 (live verification — mark, don't build)

- **First live OTLP export.** `configure_tracing()`'s exporter and the CC-subprocess env inheritance are only unit-tested against in-memory/fake here. T7's `scripts/run_phase0.py` is where they run against real `langfuse-web:3000`. If the export URL/auth is wrong, the fix is there.
- **The stretch goal — per-tool-call spans.** Per phase0-plan T6: verify in T7's first live run that Claude Code's OTel export emits *trace spans* (not only metrics/logs). If it's metrics/logs only, per-call spans are unreachable via env vars and the stretch goal is dead — step-level spans (this plan) remain the honest, met exit criterion.

## Exit criterion mapping

phase0-plan T6 "step-level spans + usage from `tracing.py` guaranteed" → Task 2 (`step_span` + poll-loop wiring, proven by the in-memory span-capture test). "OTel → local self-hosted Langfuse … point `.env` at the existing local instance" → Task 1 (`_langfuse_otlp_config` + `configure_tracing`, endpoint/auth derivation proven by unit test; env inheritance for the T5 subprocess proven by `test_configure_tracing_publishes_otel_env_for_subprocess`).
