# T7 — Phase 0 Demo Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/run_phase0.py`, the throwaway runner that seeds a task, drives one Claude Code agent through the poll loop, waits for `run.complete` under a timeout, and dumps the events table — the first live end-to-end Phase 0 run.

**Architecture:** A single `main()` coroutine wires the existing pieces (`tracing.configure_tracing`, `log.emit/read_events`, `ClaudeCodeAgent`, `poll_loop.run_agent`). Completion is detected by polling the log for a `run.complete` event under `asyncio.wait_for`. A small `shutdown_tracing()` is added to `observability/tracing.py` so step-level spans flush to Langfuse before the short process exits.

**Tech Stack:** Python 3.12, asyncio, asyncpg (via `substrate.log`), OpenTelemetry SDK, Claude Code CLI (`claude -p`), Docker Compose.

## Global Constraints

- Python `>=3.12`; no new third-party dependencies (stdlib + already-declared deps only).
- `scripts/run_phase0.py` is throwaway — absorbed by `orchestrator.py` in Phase 1. Do not change the substrate, poll loop, or Claude Code runtime contracts.
- The Claude Code runtime returns `emits=[]`; the **model** emits `claim.made` / `run.complete` through the substrate MCP. `run_phase0.py` must not emit those itself.
- Seed goal: `"What is the capital of France?"`. Run timeout: `120.0` seconds.
- Test style: monkeypatch `substrate.log` functions with in-memory doubles (see `tests/test_poll_loop.py`); set `tracing` module globals directly (see `tests/test_tracing.py`). `pytest.ini` has `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed.

---

### Task 1: Add `shutdown_tracing()` to flush spans on exit

**Files:**
- Modify: `observability/tracing.py` (add `_provider` global + `shutdown_tracing()`; store provider in `configure_tracing()`)
- Test: `tests/test_tracing.py` (append two tests)

**Interfaces:**
- Consumes: existing `tracing.configure_tracing() -> bool`, module globals `tracing._tracer`, `tracing._provider`.
- Produces: `tracing.shutdown_tracing() -> None` — force-flushes pending spans via the stored `TracerProvider`; no-op when tracing was never configured. Task 2 calls it in `main()`'s `finally`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracing.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tracing.py::test_shutdown_tracing_noop_when_unconfigured tests/test_tracing.py::test_shutdown_tracing_flushes_provider -v`
Expected: FAIL — `AttributeError: module 'observability.tracing' has no attribute '_provider'` / `shutdown_tracing`.

- [ ] **Step 3: Add the `_provider` global**

In `observability/tracing.py`, just below the `_tracer` definition (after line 35), add:

```python
# Kept so shutdown_tracing() can force-flush pending spans before a short
# process (T7's run_phase0.py) exits and drops them. None until configured.
_provider: TracerProvider | None = None
```

- [ ] **Step 4: Store the provider in `configure_tracing()`**

In `configure_tracing()`, change the `global` line and store the provider. Replace:

```python
    global _tracer
```
with:
```python
    global _tracer, _provider
```

And after `_tracer = provider.get_tracer(__name__)`, add:

```python
    _provider = provider
```

- [ ] **Step 5: Add `shutdown_tracing()`**

Add at the end of `observability/tracing.py`:

```python
def shutdown_tracing() -> None:
    """Force-flush pending step spans to Langfuse and stop the exporter.

    BatchSpanProcessor exports on a delay, so a short-lived process can exit
    before the last spans are sent. provider.shutdown() flushes synchronously.
    No-op when configure_tracing() never installed a provider (local/tests).
    """
    if _provider is not None:
        _provider.shutdown()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_tracing.py -v`
Expected: PASS (all tracing tests, including the two new ones).

- [ ] **Step 7: Commit**

```bash
git add observability/tracing.py tests/test_tracing.py
git commit -m "feat(t7): shutdown_tracing() to force-flush step spans on exit

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `scripts/run_phase0.py` demo runner

**Files:**
- Create: `scripts/run_phase0.py`
- Create: `tests/test_run_phase0.py`

**Interfaces:**
- Consumes: `tracing.configure_tracing()`, `tracing.shutdown_tracing()` (Task 1), `log.emit(agent, type, payload, run_id, ...)`, `log.read_events(run_id, since_id=, types=, exclude_agent=, limit=, ...)`, `log.close()`, `Role(name, subscribes_to, prompt)`, `ClaudeCodeAgent(role, run_id, runner=None)`, `claude_code._run_claude`, `poll_loop.run_agent(agent, cursor=0)`.
- Produces: `main() -> None` (async), `_wait_for_complete(run_id) -> dict`, `_dump_events(run_id) -> None`, constants `SEED_GOAL`, `RUN_TIMEOUT_S`, `WORKER_PROMPT`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_run_phase0.py`:

```python
"""T7 check: run_phase0 orchestration — seed, drive one agent, wait for
run.complete, dump. Uses an in-memory log double + a fake claude runner that
simulates the model emitting through the MCP."""
from agent.runtimes import claude_code
from substrate import log


def _install_memory_log(monkeypatch):
    """List-backed log double matching the subset of log.* run_phase0 uses."""
    store: list[dict] = []

    async def fake_emit(agent, type, payload, run_id, reply_to=None, correlation=None):
        store.append({"id": len(store) + 1, "agent": agent, "type": type,
                      "payload": payload, "run_id": run_id})
        return {"id": len(store), "ts": 0.0}

    async def fake_read(run_id, since_id=0, types=None, correlation=None,
                        limit=50, exclude_agent=None):
        out = [e for e in store
               if e["run_id"] == run_id and e["id"] > since_id
               and (types is None or e["type"] in types)
               and (exclude_agent is None or e["agent"] != exclude_agent)]
        return out[:limit]

    async def fake_close():
        pass

    monkeypatch.setattr(log, "emit", fake_emit)
    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "close", fake_close)
    return store


async def test_run_phase0_completes_and_records_answer(monkeypatch):
    from scripts import run_phase0

    store = _install_memory_log(monkeypatch)

    async def fake_runner(prompt, env):
        # Simulate the model emitting through the substrate MCP during the step.
        await log.emit(env["AGENT_NAME"], "claim.made", {"answer": "Paris"},
                       run_id=env["RUN_ID"])
        await log.emit(env["AGENT_NAME"], "run.complete", {}, run_id=env["RUN_ID"])
        return {"usage": {"input_tokens": 1}}

    monkeypatch.setattr(claude_code, "_run_claude", fake_runner)

    await run_phase0.main()

    types = [e["type"] for e in store]
    assert "run.start" in types
    assert "task.created" in types
    assert "claim.made" in types
    assert "run.complete" in types
    assert "agent.step" in types  # the harness recorded the step
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_run_phase0.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts'` (or `scripts.run_phase0`).

- [ ] **Step 3: Create the package marker for `scripts/`**

Create `scripts/__init__.py` (empty file) so `from scripts import run_phase0` imports in the test. (The container runs it as `python scripts/run_phase0.py`, which does not need the package, but the test import does.)

```python
```

- [ ] **Step 4: Write `scripts/run_phase0.py`**

Create `scripts/run_phase0.py`:

```python
"""T7 — throwaway Phase 0 demo runner (phase0-plan T7).

First LIVE `claude -p` run: seeds one task, drives one Claude Code agent through
the shared poll loop, waits for the model's `run.complete` under a timeout, then
dumps the events table. Validates the CLI + config/claude/.mcp.json + OTel wiring
end to end. Absorbed by orchestrator.py in Phase 1.

Run in-container: `docker compose up` (kernel command is this script). Auth for
the in-container `claude` comes from CLAUDE_CODE_OAUTH_TOKEN in .env (Pro-plan
token from `claude setup-token`).
"""
import asyncio
import contextlib
import uuid

from agent import poll_loop
from agent.role import Role
from agent.runtimes.claude_code import ClaudeCodeAgent
from observability import tracing
from substrate import log

SEED_GOAL = "What is the capital of France?"
RUN_TIMEOUT_S = 120.0

WORKER_PROMPT = (
    "You are a worker agent in a multi-agent system. When you see a "
    "'task.created' event, answer the question in its payload's 'goal' field. "
    "Emit your answer as a 'claim.made' event with payload {\"answer\": <your "
    "answer>} using the emit_event tool, then emit a 'run.complete' event to "
    "signal the episode is done."
)


async def _wait_for_complete(run_id: str) -> dict:
    """Poll the log until a run.complete event exists; return it."""
    while True:
        done = await log.read_events(run_id=run_id, types=["run.complete"])
        if done:
            return done[0]
        await asyncio.sleep(0.5)


async def _dump_events(run_id: str) -> None:
    """Print the run's full event log — the replayable record (arch §4)."""
    events = await log.read_events(run_id=run_id, limit=500)
    print(f"\n=== events for run {run_id} ({len(events)}) ===")
    for e in events:
        print(f"{e['id']:>4} | {e['agent']:<8} | {e['type']:<14} | {e['payload']}")


async def main() -> None:
    tracing.configure_tracing()  # first live OTLP export call site
    run_id = uuid.uuid4().hex
    print(f"run_id={run_id} goal={SEED_GOAL!r}")

    await log.emit("kernel", "run.start", {"goal": SEED_GOAL}, run_id=run_id)
    await log.emit("kernel", "task.created", {"goal": SEED_GOAL}, run_id=run_id)

    agent = ClaudeCodeAgent(
        Role(name="worker", subscribes_to=["task.created"], prompt=WORKER_PROMPT),
        run_id,
    )
    loop_task = asyncio.create_task(poll_loop.run_agent(agent))
    try:
        await asyncio.wait_for(_wait_for_complete(run_id), timeout=RUN_TIMEOUT_S)
        print("run.complete received")
    except asyncio.TimeoutError:
        print(f"TIMEOUT after {RUN_TIMEOUT_S}s — agent wedged, dumping anyway")
    finally:
        agent.stop()
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
        tracing.shutdown_tracing()  # flush step spans to Langfuse before exit
        await _dump_events(run_id)
        await log.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_run_phase0.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `python -m pytest -q`
Expected: PASS — all prior tests plus the new T7 tests. (Real-DB integration tests may be skipped/fail only if no Postgres is running; that is pre-existing environment behavior, not caused by this task.)

- [ ] **Step 7: Commit**

```bash
git add scripts/__init__.py scripts/run_phase0.py tests/test_run_phase0.py
git commit -m "feat(t7): scripts/run_phase0.py Phase 0 demo runner + test

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Wire the demo into compose + document Pro-plan auth

**Files:**
- Modify: `docker-compose.yml` (kernel `command`)
- Modify: `.env.example` (add `CLAUDE_CODE_OAUTH_TOKEN`)

**Interfaces:**
- Consumes: `scripts/run_phase0.py` from Task 2.
- Produces: no code interface — config/docs so `docker compose up` runs the demo with Pro-plan auth.

- [ ] **Step 1: Point the kernel at the demo runner**

In `docker-compose.yml`, replace the kernel command line:

```yaml
    # T7 swaps this for: python scripts/run_phase0.py
    command: ["sleep", "infinity"]
```
with:
```yaml
    command: ["python", "scripts/run_phase0.py"]
```

- [ ] **Step 2: Document the Pro-plan auth route in `.env.example`**

In `.env.example`, replace the auth block:

```
# Claude Code CLI auth: an API key, or a long-lived token from `claude setup-token`.
ANTHROPIC_API_KEY=
```
with:
```
# Claude Code CLI auth. The demo runs in-container (docker compose up), where your
# personal ~/.claude login is NOT mounted, so the container's claude needs one of:
#   - CLAUDE_CODE_OAUTH_TOKEN: Pro-plan route. Run `claude setup-token` on the host
#     and paste the token here — usage counts against your Claude subscription, no
#     pay-per-token key needed.
#   - ANTHROPIC_API_KEY: pay-as-you-go API key (separate billing).
CLAUDE_CODE_OAUTH_TOKEN=
ANTHROPIC_API_KEY=
```

- [ ] **Step 3: Verify the compose file still parses**

Run: `docker compose config --quiet && echo OK`
Expected: `OK` (no YAML/schema error). If `docker` is unavailable in the environment, skip and note it — this is config-only.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "feat(t7): run demo in-container + document Pro-plan auth token

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Mark T7 done in the phase0 plan

**Files:**
- Modify: `docs/phase0-plan.md` (T7 heading + sequencing note)

**Interfaces:** none (docs only).

- [ ] **Step 1: Mark the T7 section done**

In `docs/phase0-plan.md`, change the heading:

```
### T7 — `scripts/run_phase0.py` (throwaway)
```
to:
```
### T7 — `scripts/run_phase0.py` (throwaway) ✅ done
```

- [ ] **Step 2: Commit**

```bash
git add docs/phase0-plan.md
git commit -m "docs(t7): mark T7 demo runner complete in phase0-plan

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Live-run checklist (owner drives after merge; per phase0-plan T7)

Not part of the automated deliverable. After `docker compose up` with
`CLAUDE_CODE_OAUTH_TOKEN` set in `.env`:

1. **Env propagation:** `claude` passes `AGENT_NAME` / `RUN_ID` to the stdio MCP
   child — check the stamped `agent` column on emitted events (worker, not None).
2. **Interpreter resolves `substrate`:** `.mcp.json`'s bare `command: "python"`
   (cwd `/app`) can `import substrate`. Pin the interpreter / set `cwd` if not.
3. **No trust prompt:** `-p` + `--mcp-config` + `--allowedTools` loads the server
   without an interactive permission prompt.
4. **Langfuse:** the run's `agent.step` spans appear in the UI (stretch:
   per-tool-call spans from the subprocess too).
