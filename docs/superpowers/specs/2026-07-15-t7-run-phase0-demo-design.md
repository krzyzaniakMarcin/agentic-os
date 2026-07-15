# T7 — `scripts/run_phase0.py` (Phase 0 demo runner)

**Status:** approved design
**Date:** 2026-07-15
**Task:** phase0-plan T7 — wire the demo last

## Purpose

The first **live** end-to-end run of the Phase 0 stack. Everything below T7 is
unit-tested against fakes; this script is where the real `claude -p` subprocess,
`config/claude/.mcp.json`, the substrate MCP, the poll loop, and the OTel →
Langfuse export all run together for the first time. It is a throwaway,
absorbed by `orchestrator.py` in Phase 1.

**Exit criterion it satisfies (phase0-plan):** `docker compose up` →
`run_phase0.py` → the agent reads the seed task, emits `claim.made` (the answer)
+ `run.complete`; every step shows in **Langfuse** and the **`events` table**,
which holds complete `agent.step` records (`saw_events` + `usage`).

## Scope

**In:** `scripts/run_phase0.py`, one required addition to
`observability/tracing.py` (`shutdown_tracing()`), `docker-compose.yml` kernel
command, `.env.example` auth line, one unit test.

**Out:** actually running `docker compose up` live (the owner drives that). No
changes to the substrate, poll loop, or Claude Code runtime contracts.

## Component & flow

`scripts/run_phase0.py`:

```
async def main():
    tracing.configure_tracing()          # FIRST live call site of the OTLP export
    run_id = uuid.uuid4().hex
    await log.emit("kernel", "run.start",    {"goal": SEED_GOAL}, run_id=run_id)
    await log.emit("kernel", "task.created", {"goal": SEED_GOAL}, run_id=run_id)

    agent = ClaudeCodeAgent(Role(name="worker",
                                 subscribes_to=["task.created"],
                                 prompt=WORKER_PROMPT),
                            run_id)
    loop_task = asyncio.create_task(poll_loop.run_agent(agent))
    try:
        await asyncio.wait_for(_wait_for_complete(run_id), timeout=RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        print(f"TIMEOUT after {RUN_TIMEOUT_S}s — agent wedged, dumping anyway")
    finally:
        agent.stop()
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
        tracing.shutdown_tracing()        # force-flush pending step spans to Langfuse
        await _dump_events(run_id)
        await log.close()
```

- `_wait_for_complete(run_id)` — polls `log.read_events(run_id, types=["run.complete"])`
  every 0.5s, returns the event once present. Kept a separate function so it is
  testable in isolation.
- `_dump_events(run_id)` — `log.read_events(run_id, limit=500)`, prints one line
  per event: `id | agent | type | payload`.

Constants: `RUN_TIMEOUT_S = 120.0`, `SEED_GOAL = "What is the capital of France?"`.

### Why polling for `run.complete` (not a callback or self-stopping agent)

Matches phase0-plan's exact wording ("wait for `run.complete` under an
`asyncio.wait_for` timeout"). A callback would mean changing `run_agent`'s
signature for a throwaway; a self-stopping agent would need it to subscribe to
`run.complete`, adding coupling. Polling is the smallest correct option.

### Ordering (why cancelling the loop is clean)

The model emits `claim.made` + `run.complete` through the substrate MCP *during*
the subprocess step. The subprocess returns, `step()` returns `([], usage)`, the
loop emits `agent.step`, then loops back, finds no new `task.created`, and sleeps
`tick_s`. By the time `_wait_for_complete` sees `run.complete`, the step has
finished and the loop is idle — cancelling a sleeping task is clean.

## The worker role prompt

Instructs the model: when you see a `task.created` event, answer the question in
its payload's `goal` field; emit your answer as a `claim.made` event
(`payload: {"answer": <answer>}`) using the `emit_event` tool, then emit a
`run.complete` event to end the episode. The model does the emitting — `step()`
returns `emits=[]` per the CC runtime contract.

## Required addition to `observability/tracing.py`

`configure_tracing()` today does not keep a reference to the `TracerProvider`.
Its `BatchSpanProcessor` exports on a ~5s delay, so the step-level spans a short
demo produces can be dropped when the process exits before the next flush —
which would break the "every step shows in Langfuse" criterion.

Change:
- Store the provider in a module global inside `configure_tracing()`.
- Add `shutdown_tracing()` that calls `provider.shutdown()` (force-flushes
  pending spans); no-op when tracing was never configured.

The `claude -p` subprocess flushes its own spans on its own process exit, so
only the Python-side step spans need this.

## Supporting changes

- **`docker-compose.yml`:** kernel `command: ["sleep", "infinity"]` →
  `command: ["python", "scripts/run_phase0.py"]` (the existing comment already
  anticipates this swap).
- **`.env.example`:** add `CLAUDE_CODE_OAUTH_TOKEN=` with a note that it is the
  Pro-plan auth route (`claude setup-token` on the host), needed because the demo
  runs in-container where the personal `~/.claude` login is not mounted.

## Testing

One test, `tests/test_run_phase0.py`, following the existing monkeypatch style
(see `tests/test_poll_loop.py`):

- In-memory log double: monkeypatch `log.emit` / `log.read_events` /
  `log.close` with a simple list-backed store.
- Monkeypatch `claude_code._run_claude` with a fake runner that simulates the
  model — it emits `claim.made` + `run.complete` into the store, returns a usage
  dict.
- Run `main()`; assert: it returns (no timeout), both `claim.made` and
  `run.complete` are in the store, and shutdown/close were reached.

`configure_tracing()` returns False in the test (no Langfuse creds), so
`shutdown_tracing()` is a no-op — the export path is not exercised in tests (it
is the whole point of the live run).

## Live-run checklist (owner drives; per phase0-plan T7)

Run after `docker compose up` (with `CLAUDE_CODE_OAUTH_TOKEN` in `.env`):

1. **Env propagation:** the `claude` process passes `AGENT_NAME` / `RUN_ID` down
   to the stdio MCP child — check the stamped `agent` column on emitted events.
2. **Interpreter resolves `substrate`:** `config/claude/.mcp.json` uses bare
   `command: "python"`; confirm the resolved interpreter (cwd `/app`) can import
   `substrate`. Pin the interpreter / set `cwd` if not.
3. **No trust prompt:** `-p` + `--mcp-config` + `--allowedTools` loads the server
   without an interactive permission prompt.
4. **Langfuse:** the run's `agent.step` spans (and, stretch, per-tool-call spans
   from the subprocess) appear in the Langfuse UI.
