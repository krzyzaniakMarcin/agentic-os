# T10 — `kernel/orchestrator.py` (the dumb kernel) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `kernel/orchestrator.py` — `run_episode(cfg)` seeds a run, spawns one poll loop per role, supervises rails (budget breach → `system.halt`, `run.complete` → stop), and returns a projection over the event log — then repoint the docker `kernel` command at it and retire `scripts/run_phase0.py`.

**Architecture:** The orchestrator is the *dumb kernel* (phase1-plan §T10): it never inspects event content or decides who works next — routing stays purely the `types` filter each `poll_loop.run_agent` carries (arch §6). `run_episode` only drives rails. T10 lands **before** T11 (termination) and T12 (budget), so it exposes two seams they fill later: a local `run.complete`-only termination check (T11 swaps in quiescence) and an injected `cfg["budget"]` object with `async breached()` (T12 supplies the real one). The config is a hardcoded stub dict mirroring `run_phase0.py`; T14 replaces it with the YAML loader.

**Tech Stack:** Python 3.12+, asyncio, asyncpg (via `substrate.log`), pytest + pytest-asyncio (`asyncio_mode = "auto"`). No new dependencies.

## Global Constraints

- `requires-python = ">=3.12"`.
- **No new dependencies** — reuse `substrate.log`, `agent.poll_loop`, `agent.role.load_role`, `agent.runtimes.claude_code.ClaudeCodeAgent`, `observability.tracing`.
- **Kernel never inspects event content** (arch §6): supervise rails only; no content-based routing or "who works next" decisions.
- **`poll_loop.run_agent` dispatch semantics unchanged** — T10 adds no hooks to the loop or `base.py`.
- Tests are plain `async def test_*` (asyncio auto-mode); no framework scaffolding beyond the in-memory log double.
- ponytail: mark deliberate shortcuts with a `# ponytail:` comment naming the ceiling + upgrade path.

## Interfaces this plan produces (consumed by T11/T12/T14)

- `run_episode(cfg: dict, *, run_id: str | None = None) -> list[dict]` — returns the run's event-log dump.
  - `cfg` keys: `goal: str`, `roles: list[dict]` (each → `role.load_role` → `ClaudeCodeAgent`), optional `budget` (object with `async breached() -> str | None`; T12 supplies), optional `tick_s: float` (supervise-loop poll, default `0.5`), optional `run_timeout_s: float | None` (wall-clock guard, default `300.0`; T12 folds into budget).
- `summarize(run_id: str) -> list[dict]` — projection over the log (arch §4).
- `_run_complete(run_id) -> bool` — T10 termination seam; **T11 replaces the call site** with `kernel/termination.terminated(...)`.

---

## File Structure

- **Create** `kernel/__init__.py` — new package (empty).
- **Create** `kernel/orchestrator.py` — `run_episode`, `summarize`, `_run_complete`, `_drain`, stub cfg + `main()`/`__main__` (docker `kernel` command).
- **Create** `tests/test_orchestrator.py` — self-check for `run_episode` (run.complete terminates; budget breach halts + stops all).
- **Modify** `pyproject.toml:21` — add `"kernel"` to `[tool.setuptools] packages`.
- **Modify** `docker-compose.yml:32` — `command` → `["python", "-m", "kernel.orchestrator"]`.
- **Delete** `scripts/run_phase0.py` — absorbed by the orchestrator.
- **Delete** `tests/test_run_phase0.py` — replaced by `tests/test_orchestrator.py`.

---

## Task 1: Orchestrator core (`run_episode` + `summarize`)

**Files:**
- Create: `kernel/__init__.py`
- Create: `kernel/orchestrator.py`
- Test: `tests/test_orchestrator.py`
- Modify: `pyproject.toml:21`

**Interfaces:**
- Consumes: `substrate.log.emit(agent, type, payload, run_id, reply_to=None, correlation=None) -> dict`, `substrate.log.read_events(run_id, since_id=0, types=None, correlation=None, limit=50, exclude_agent=None) -> list[dict]`; `agent.poll_loop.run_agent(agent, cursor=0)`; `agent.role.load_role(dict) -> Role`; `agent.runtimes.claude_code.ClaudeCodeAgent(role, run_id, runner=None)`.
- Produces: `run_episode`, `summarize`, `_run_complete` (see interface block above).

- [ ] **Step 1: Create the `kernel` package**

Create `kernel/__init__.py` (empty file):

```python
```

- [ ] **Step 2: Add `kernel` to the packages list**

In `pyproject.toml:21`, change:

```toml
packages = ["substrate", "agent", "agent.runtimes", "observability"]
```

to:

```toml
packages = ["substrate", "agent", "agent.runtimes", "observability", "kernel"]
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_orchestrator.py`. This ports the proven in-memory-log double from the old `test_run_phase0.py` and adds the budget-breach case. Note the fake `_run_claude` is patched **before** `run_episode` builds agents, so `ClaudeCodeAgent.__init__`'s `runner or _run_claude` binds the fake (same ordering the old T7 test relied on).

```python
"""T10 check: orchestrator run_episode — seed, spawn loops, supervise rails,
dump. Uses an in-memory log double + a fake claude runner (no model)."""
from agent.runtimes import claude_code
from agent.runtimes.claude_code import ClaudeCodeAgent
from kernel import orchestrator
from substrate import log


def _install_memory_log(monkeypatch):
    """List-backed log double matching the subset of log.* the kernel uses."""
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


async def test_run_complete_terminates_and_records(monkeypatch):
    store = _install_memory_log(monkeypatch)

    async def fake_runner(prompt, env):
        # Simulate the model emitting through the substrate MCP during the step.
        await log.emit(env["AGENT_NAME"], "claim.made", {"answer": "Paris"},
                       run_id=env["RUN_ID"])
        await log.emit(env["AGENT_NAME"], "run.complete", {}, run_id=env["RUN_ID"])
        return {"usage": {"input_tokens": 1}}

    monkeypatch.setattr(claude_code, "_run_claude", fake_runner)

    cfg = {"goal": "What is the capital of France?",
           "roles": [{"name": "worker", "subscribes_to": ["task.created"],
                      "prompt": "answer it"}]}
    events = await orchestrator.run_episode(cfg)

    types = [e["type"] for e in events]
    for expected in ("run.start", "task.created", "claim.made",
                     "run.complete", "agent.step"):
        assert expected in types


async def test_budget_breach_halts_and_stops_all(monkeypatch):
    _install_memory_log(monkeypatch)

    captured: list[ClaudeCodeAgent] = []

    class Spy(ClaudeCodeAgent):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.append(self)

    monkeypatch.setattr(orchestrator, "ClaudeCodeAgent", Spy)

    async def fake_runner(prompt, env):  # emits nothing → no run.complete
        return {"usage": {"input_tokens": 1}}

    monkeypatch.setattr(claude_code, "_run_claude", fake_runner)

    class FakeBudget:
        async def breached(self):
            return "usd_budget"

    cfg = {"goal": "g",
           "roles": [{"name": "w1", "subscribes_to": ["task.created"], "prompt": "p"},
                     {"name": "w2", "subscribes_to": ["task.created"], "prompt": "p"}],
           "budget": FakeBudget(), "tick_s": 0.0}
    events = await orchestrator.run_episode(cfg)

    halt = [e for e in events if e["type"] == "system.halt"]
    assert halt and halt[0]["payload"]["reason"] == "usd_budget"
    assert captured and all(a.stopped for a in captured)
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kernel.orchestrator'` (or `AttributeError` on `orchestrator.run_episode`).

- [ ] **Step 5: Implement `kernel/orchestrator.py`**

Create `kernel/orchestrator.py`:

```python
"""kernel/orchestrator.py — the dumb kernel (phase1-plan §T10).

run_episode(cfg): emit run.start + one seed task.created, spawn one poll loop
per role, then supervise RAILS ONLY — a budget breach becomes system.halt +
stop; run.complete (T11 adds quiescence) stops the fleet. Returns a projection
over the event log. Absorbs scripts/run_phase0.py: this module is the
docker-compose `kernel` command (`python -m kernel.orchestrator`).

The kernel never inspects event content or decides who works next — routing is
purely the `types` filter each loop carries (arch §6). Rails, nothing more.
"""
import asyncio
import contextlib
import time
import uuid

from agent import poll_loop
from agent.role import load_role
from agent.runtimes.claude_code import ClaudeCodeAgent
from observability import tracing
from substrate import log

# ponytail: standalone wall-clock guard so a wedged live model can't hang the
# demo before T12 exists. T12 folds this into budget.breached() (usd + wall-
# clock + kill switch), and T15 makes it exclude paused time — the `elapsed`
# calc is factored here so that's a one-line move, not a retrofit.
_DEFAULT_RUN_TIMEOUT_S = 300.0
# Let the in-flight step() return so the loop records its agent.step before we
# tear down — the exit criterion wants that record (P0 drain discipline).
_DRAIN_TIMEOUT_S = 30.0


async def _run_complete(run_id: str) -> bool:
    """T10 termination seam: any run.complete ends the episode. T11 replaces
    this call site with kernel/termination.terminated (adds quiescence + a
    mid-step guard + the T15 resume-gate check)."""
    done = await log.read_events(run_id=run_id, types=["run.complete"], limit=1)
    return bool(done)


async def summarize(run_id: str) -> list[dict]:
    """Projection over the log — the replayable record (arch §4). Same 'dump the
    events table' the P0 runner did, now the kernel's job."""
    return await log.read_events(run_id=run_id, limit=500)


async def _drain(tasks: list[asyncio.Task]) -> None:
    """Agents already stop()'d; await each loop so its in-flight step() records
    agent.step, then cancel any wedged step (P0 drain discipline)."""
    for t in tasks:
        try:
            await asyncio.wait_for(t, timeout=_DRAIN_TIMEOUT_S)
        except asyncio.TimeoutError:  # wedged step — stop waiting and tear down
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t


async def run_episode(cfg: dict, *, run_id: str | None = None) -> list[dict]:
    run_id = run_id or uuid.uuid4().hex
    goal = cfg["goal"]
    await log.emit("kernel", "run.start", {"goal": goal}, run_id=run_id)
    await log.emit("kernel", "task.created", {"goal": goal}, run_id=run_id)

    agents = [ClaudeCodeAgent(load_role(r), run_id) for r in cfg["roles"]]
    tasks = [asyncio.create_task(poll_loop.run_agent(a)) for a in agents]

    budget = cfg.get("budget")  # T12 supplies an object with async breached()->str|None
    tick_s = cfg.get("tick_s", 0.5)
    timeout_s = cfg.get("run_timeout_s", _DEFAULT_RUN_TIMEOUT_S)
    started = time.monotonic()
    try:
        while True:
            if budget is not None and (reason := await budget.breached()):
                await log.emit("kernel", "system.halt", {"reason": reason}, run_id=run_id)
                break
            elapsed = time.monotonic() - started  # T15: subtract paused time here
            if timeout_s is not None and elapsed > timeout_s:
                await log.emit("kernel", "system.halt", {"reason": "timeout"}, run_id=run_id)
                break
            if await _run_complete(run_id):  # T11 swaps in quiescence-aware terminated()
                break
            await asyncio.sleep(tick_s)
    finally:
        for a in agents:
            a.stop()  # loops exit once their in-flight step() finishes + records agent.step
        await _drain(tasks)
    return await summarize(run_id)


# --- docker `kernel` command: hardcoded stub cfg (T14 replaces with YAML) -----

_STUB_GOAL = "What is the capital of France?"
_WORKER_PROMPT = (
    "You are a worker agent in a multi-agent system. When you see a "
    "'task.created' event, answer the question in its payload's 'goal' field. "
    "Emit your answer as a 'claim.made' event with payload {\"answer\": <your "
    "answer>} using the emit_event tool, then emit a 'run.complete' event to "
    "signal the episode is done."
)


def _stub_cfg() -> dict:
    """Hardcoded run config mirroring run_phase0 (phase1-plan §T10). T14 replaces
    this with topologies/supervisor.yaml + a loader. Defines the cfg *shape*
    run_episode consumes; T14 supplies the parser."""
    return {
        "goal": _STUB_GOAL,
        "roles": [
            {"name": "worker", "subscribes_to": ["task.created"], "prompt": _WORKER_PROMPT},
        ],
    }


async def main() -> None:
    tracing.configure_tracing()  # first live OTLP export call site (absorbs run_phase0)
    cfg = _stub_cfg()
    print(f"goal={cfg['goal']!r}")
    try:
        events = await run_episode(cfg)
        print(f"\n=== events ({len(events)}) ===")
        for e in events:
            print(f"{e['id']:>4} | {e['agent']:<8} | {e['type']:<14} | {e['payload']}")
    finally:
        tracing.shutdown_tracing()  # flush step spans to Langfuse before exit
        await log.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Commit**

```bash
git add kernel/__init__.py kernel/orchestrator.py tests/test_orchestrator.py pyproject.toml
git commit -m "feat(t10): kernel/orchestrator.py — run_episode + supervise rails"
```

---

## Task 2: Absorb `run_phase0.py` (docker wiring + deletions)

**Files:**
- Modify: `docker-compose.yml:32`
- Delete: `scripts/run_phase0.py`
- Delete: `tests/test_run_phase0.py`

**Interfaces:**
- Consumes: `kernel/orchestrator.py` `main()`/`__main__` from Task 1.
- Produces: docker `kernel` command runs `python -m kernel.orchestrator`.

- [ ] **Step 1: Repoint the docker `kernel` command**

In `docker-compose.yml:32`, change:

```yaml
    command: ["python", "-m", "scripts.run_phase0"]
```

to:

```yaml
    command: ["python", "-m", "kernel.orchestrator"]
```

- [ ] **Step 2: Delete the retired runner and its test**

```bash
git rm scripts/run_phase0.py tests/test_run_phase0.py
```

- [ ] **Step 3: Verify no stale references to `run_phase0` remain**

Run: `grep -rn "run_phase0" --include='*.py' --include='*.yml' --include='*.toml' .`
Expected: no output (empty). If `scripts` is now empty except `__init__.py`, leave `scripts/__init__.py` — nothing else references it and removing it is out of scope.

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS — all tests green (orchestrator tests present, `test_run_phase0` gone).

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(t10): docker kernel command → kernel.orchestrator; retire run_phase0"
```

---

## Self-Review

**1. Spec coverage** (phase1-plan §T10 bullets):
- `run_episode(cfg)`: emit run.start + one seed task.created, one `ClaudeCodeAgent` per role, `asyncio.create_task(run_agent(a))` per agent, then supervise loop → **Task 1, Step 5**. ✅
- Supervise rails only; budget breach → `system.halt` + `a.stop()` every agent; no content routing → **Task 1, Step 5** (`budget.breached()` seam + halt + stop-all in `finally`). ✅
- Detect termination (`run.complete` or quiescence) → **Task 1** `_run_complete` seam, with an inline note that **T11 swaps the call site** for quiescence-aware `terminated()`. ✅ (quiescence itself is T11's task, not T10.)
- Return a projection (`summarize(run_id)`) → **Task 1, Step 5** `summarize`. ✅
- Absorbs `run_phase0.py` → docker `kernel` command + P0 drain-before-teardown → **Task 1** (`_drain`, `main`) + **Task 2** (docker repoint, deletion). ✅
- Config dependency: hardcoded stub `cfg` dict now, T14 supplies the loader → **Task 1** `_stub_cfg`, cfg-shape documented in the interfaces block. ✅
- Self-check: two agents (no model) + stub cfg, run.complete terminates, `system.halt` on forced budget breach, assert agents stopped + halt in log → **Task 1, Step 3** (`test_budget_breach_halts_and_stops_all` asserts `system.halt` reason **and** `all(a.stopped ...)`; `test_run_complete_terminates_and_records` covers the run.complete path). ✅

**2. Placeholder scan:** No TBD/TODO/"handle edge cases". Every code step shows full content. The two `# ponytail:` / T11 / T12 / T15 notes are deliberate forward-references to sibling tasks, not deferred work inside T10. ✅

**3. Type consistency:** `run_episode(cfg, *, run_id=None) -> list[dict]`, `summarize(run_id) -> list[dict]`, `_run_complete(run_id) -> bool`, `_drain(tasks)`; cfg keys (`goal`, `roles`, `budget`, `tick_s`, `run_timeout_s`) match between the interfaces block, the implementation, and the tests. `budget.breached()` is async and returns `str | None` in both the FakeBudget test and the call site. `ClaudeCodeAgent(role, run_id, runner=None)` matches the real signature; the `Spy` subclass forwards `*a, **k`. ✅
