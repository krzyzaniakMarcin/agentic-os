# T11 — Quiescence Termination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the kernel a quiescence-aware termination check (`kernel/termination.py`) that ends an episode on `run.complete` **or** a genuine idle period — without truncating a run whose agents are mid-step or merely rate-limit-paused.

**Architecture:** A small stateful `Terminator` object, created by the orchestrator after it spawns the poll loops, is polled once per tick. It terminates on any `run.complete` event, else on quiescence: no new events for `quiescence_s` **and** no agent currently `in_step` **and** the fleet not paused (T15 resume-gate). The poll loop sets a new `in_step` flag on `Agent` around its `await agent.step(...)` call so the kernel knows who is working. `Terminator` replaces the T10 `_run_complete` seam in `run_episode`.

**Tech Stack:** Python 3.13, asyncio, pytest (`uv run pytest`), append-only event log (`substrate/log.py`).

## Global Constraints

- Kernel supervises **rails only** — it never inspects event *content* or decides who works next; routing stays the `types` filter (arch §6). `run.complete` / `agent.step` are structural, not content.
- The poll loop's dispatch/read/self-exclude/emit behavior is **unchanged**; T11 adds only an `in_step` flag hook (plan §Dispatch).
- Quiescence must use a **cheap probe** — `read_events(since_id=last_seen, limit=1)` per tick, never a full scan.
- A rate-limit pause is **not** quiescence: a closed resume-gate counts as "busy". T15 does not exist yet — expose it as an injectable `paused` callable defaulting to "never paused"; do **not** build T15's gate here (YAGNI).
- Tests use fake agents + a fake clock + the in-memory log double already used in `tests/test_orchestrator.py`. No live model, no real DB.
- Run the suite with `uv run pytest` (pythonpath=`.` is set in `pyproject.toml`).

---

## File Structure

- **Create** `kernel/termination.py` — the `Terminator` class (run.complete + quiescence logic).
- **Modify** `agent/base.py` — add `in_step` flag to `Agent.__init__`.
- **Modify** `agent/poll_loop.py` — set/clear `in_step` around `await agent.step(...)`.
- **Modify** `kernel/orchestrator.py` — replace `_run_complete` with a `Terminator`.
- **Create** `tests/test_termination.py` — the four self-check assertions.
- **Modify** `tests/test_orchestrator.py` — none required (run.complete path still terminates via `Terminator`); verify it still passes.

---

### Task 1: `in_step` flag on the Agent (base + poll loop)

**Files:**
- Modify: `agent/base.py:18-33`
- Modify: `agent/poll_loop.py:26-30`
- Test: `tests/test_poll_loop.py`

**Interfaces:**
- Produces: `Agent.in_step: bool` — `False` at rest, `True` for the duration of a `step()` call. Read by `Terminator` (Task 2).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_poll_loop.py`. It asserts `in_step` is True *during* the step and False after. Match the file's existing fake-agent/in-memory-log style — read the top of `tests/test_poll_loop.py` first and reuse its helpers rather than re-inventing them.

```python
async def test_in_step_flag_set_during_step(monkeypatch):
    # in-memory log with one subscribed event so step() runs exactly once
    from agent.base import Agent, Emit
    from agent import poll_loop
    from substrate import log

    store = [{"id": 1, "run_id": "r", "agent": "seed", "type": "task.created", "payload": {}}]

    async def fake_read(run_id, since_id=0, types=None, correlation=None, limit=50, exclude_agent=None):
        return [e for e in store if e["id"] > since_id and (types is None or e["type"] in types)][:limit]

    async def fake_emit(agent, type, payload, run_id, reply_to=None, correlation=None):
        return {"id": 99, "ts": 0.0}

    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "emit", fake_emit)

    class Probe(Agent):
        seen_in_step = None
        async def step(self, new_events):
            Probe.seen_in_step = self.in_step  # must be True mid-step
            self.stop()
            return [], {}

    from agent.role import Role
    role = Role(name="w", subscribes_to=["task.created"], prompt="p")
    agent = Probe(role, "r")
    await poll_loop.run_agent(agent)

    assert Probe.seen_in_step is True
    assert agent.in_step is False  # cleared after the step
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_poll_loop.py::test_in_step_flag_set_during_step -v`
Expected: FAIL — `AttributeError: 'Probe' object has no attribute 'in_step'`.

(If `Role`'s constructor differs, read `agent/role.py` and adjust the `Role(...)` call — the assertion logic stays the same.)

- [ ] **Step 3: Add the flag to `Agent.__init__`**

In `agent/base.py`, inside `Agent.__init__` (near `self.step_n = 0`):

```python
        self.step_n = 0
        self.in_step = False  # True while step() runs; T11 quiescence reads this
        self._stopped = False
```

- [ ] **Step 4: Set/clear it in the poll loop**

In `agent/poll_loop.py`, wrap the `await agent.step(events)` call. Replace:

```python
            emitted, usage = await agent.step(events)
```

with:

```python
            agent.in_step = True
            try:
                emitted, usage = await agent.step(events)
            finally:
                agent.in_step = False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_poll_loop.py -v`
Expected: PASS (new test + existing ones).

- [ ] **Step 6: Commit**

```bash
git add agent/base.py agent/poll_loop.py tests/test_poll_loop.py
git commit -m "feat(t11): Agent.in_step flag set around step() for quiescence"
```

---

### Task 2: `kernel/termination.py` — the Terminator

**Files:**
- Create: `kernel/termination.py`
- Test: `tests/test_termination.py`

**Interfaces:**
- Consumes: `Agent.in_step` (Task 1); `substrate.log.read_events(run_id, since_id, types, limit)`.
- Produces:
  - `Terminator(run_id: str, agents: list, quiescence_s: float, *, clock=time.monotonic, paused: Callable[[], bool] | None = None)`
  - `async Terminator.terminated() -> bool` — polled once per tick by the orchestrator.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_termination.py`. A list-backed log double + a manual clock drive all four self-check cases (plan §T11 self-check).

```python
"""T11 check: Terminator — run.complete + quiescence with mid-step and
resume-gate guards. In-memory log double + a manual clock, no model/DB."""
from kernel import termination
from substrate import log


class FakeAgent:
    def __init__(self, in_step=False):
        self.in_step = in_step


def _install_log(monkeypatch):
    store: list[dict] = []

    async def fake_emit(agent, type, payload, run_id, reply_to=None, correlation=None):
        store.append({"id": len(store) + 1, "agent": agent, "type": type,
                      "payload": payload, "run_id": run_id})
        return {"id": len(store), "ts": 0.0}

    async def fake_read(run_id, since_id=0, types=None, correlation=None,
                        limit=50, exclude_agent=None):
        out = [e for e in store if e["run_id"] == run_id and e["id"] > since_id
               and (types is None or e["type"] in types)]
        return out[:limit]

    monkeypatch.setattr(log, "emit", fake_emit)
    monkeypatch.setattr(log, "read_events", fake_read)
    return store


class Clock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t


async def test_run_complete_terminates_immediately(monkeypatch):
    store = _install_log(monkeypatch)
    await log.emit("w", "run.complete", {}, run_id="r")
    term = termination.Terminator("r", [FakeAgent()], quiescence_s=10.0, clock=Clock())
    assert await term.terminated() is True


async def test_quiescence_fires_after_threshold(monkeypatch):
    _install_log(monkeypatch)
    clk = Clock()
    term = termination.Terminator("r", [FakeAgent()], quiescence_s=10.0, clock=clk)
    assert await term.terminated() is False   # t=0, no events yet
    clk.t = 9.0
    assert await term.terminated() is False   # under threshold
    clk.t = 10.0
    assert await term.terminated() is True     # >= quiescence_s of no events


async def test_new_event_resets_quiescence(monkeypatch):
    _install_log(monkeypatch)
    clk = Clock()
    term = termination.Terminator("r", [FakeAgent()], quiescence_s=10.0, clock=clk)
    await term.terminated()                    # baseline at t=0
    clk.t = 9.0
    await log.emit("w", "agent.step", {}, run_id="r")  # activity
    assert await term.terminated() is False    # new event → resets timer
    clk.t = 18.0
    assert await term.terminated() is False    # only 9s since the new event
    clk.t = 19.0
    assert await term.terminated() is True


async def test_in_step_agent_suppresses_quiescence(monkeypatch):
    _install_log(monkeypatch)
    clk = Clock()
    agent = FakeAgent(in_step=True)
    term = termination.Terminator("r", [agent], quiescence_s=10.0, clock=clk)
    await term.terminated()
    clk.t = 100.0
    assert await term.terminated() is False    # mid-step → never quiescent
    agent.in_step = False
    assert await term.terminated() is False    # timer only starts now
    clk.t = 110.0
    assert await term.terminated() is True


async def test_closed_resume_gate_suppresses_quiescence(monkeypatch):
    _install_log(monkeypatch)
    clk = Clock()
    paused = {"v": True}
    term = termination.Terminator("r", [FakeAgent()], quiescence_s=10.0,
                                  clock=clk, paused=lambda: paused["v"])
    await term.terminated()
    clk.t = 100.0
    assert await term.terminated() is False    # fleet paused → busy
    paused["v"] = False
    assert await term.terminated() is False    # timer starts on unpause
    clk.t = 110.0
    assert await term.terminated() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_termination.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kernel.termination'`.

- [ ] **Step 3: Implement `kernel/termination.py`**

```python
"""kernel/termination.py — quiescence-aware episode termination (phase1-plan
§T11). Terminator.terminated() ends a run on any run.complete OR on quiescence:
no new events for quiescence_s AND no agent mid-step AND the fleet not paused.

Excluding in-flight agents is the whole point — a Claude Code agent can be
minutes into a tool-using turn without emitting; naive "no events for T seconds"
fires mid-thought and truncates the run. A T15 rate-limit pause is likewise not
quiescence: `paused` (a closed resume-gate) counts as busy so a throttled fleet
isn't killed. The kernel owns the sessions, so it reads Agent.in_step directly.
"""
import time
from collections.abc import Callable

from substrate import log


class Terminator:
    def __init__(self, run_id: str, agents: list, quiescence_s: float, *,
                 clock: Callable[[], float] = time.monotonic,
                 paused: Callable[[], bool] | None = None):
        self._run_id = run_id
        self._agents = agents
        self._quiescence_s = quiescence_s
        self._clock = clock
        # T15 wires a real resume-gate here; until then the fleet is never paused.
        self._paused = paused or (lambda: False)
        self._last_id = 0
        self._idle_since = clock()  # when the fleet last went idle-and-free

    async def terminated(self) -> bool:
        # Explicit completion always wins, regardless of activity.
        if await log.read_events(run_id=self._run_id, types=["run.complete"], limit=1):
            return True
        # Cheap probe: any new event since we last looked? (plan §T11 — limit=1)
        new = await log.read_events(run_id=self._run_id, since_id=self._last_id, limit=1)
        now = self._clock()
        if new:
            self._last_id = new[-1]["id"]
            self._idle_since = now
            return False
        # No new events. Busy if any agent is mid-step or the fleet is paused —
        # reset the idle clock so quiescence measures only idle-AND-free time.
        if any(a.in_step for a in self._agents) or self._paused():
            self._idle_since = now
            return False
        return (now - self._idle_since) >= self._quiescence_s
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_termination.py -v`
Expected: PASS (all five tests).

- [ ] **Step 5: Commit**

```bash
git add kernel/termination.py tests/test_termination.py
git commit -m "feat(t11): kernel/termination.py — quiescence-aware Terminator"
```

---

### Task 3: Wire the Terminator into the orchestrator

**Files:**
- Modify: `kernel/orchestrator.py:33-38` (remove `_run_complete`), `:64-93` (`run_episode`), imports.
- Test: `tests/test_orchestrator.py` (existing run.complete test must still pass; add a quiescence test).

**Interfaces:**
- Consumes: `termination.Terminator` (Task 2); `cfg.get("quiescence_s")`.
- Produces: `run_episode` terminates on run.complete **or** quiescence.

- [ ] **Step 1: Add a failing quiescence test**

Append to `tests/test_orchestrator.py`. A run whose model emits `agent.step` but never `run.complete`, with a tiny `quiescence_s` and no budget/wall-clock trip in reach, must terminate via quiescence (no `system.halt`).

```python
async def test_quiescence_terminates_without_halt(monkeypatch):
    _install_memory_log(monkeypatch)

    async def fake_runner(prompt, env):  # emits nothing beyond the loop's agent.step
        return {"usage": {}}

    monkeypatch.setattr(claude_code, "_run_claude", fake_runner)

    cfg = {"goal": "g",
           "roles": [{"name": "w", "subscribes_to": ["task.created"], "prompt": "p"}],
           "quiescence_s": 0.0, "tick_s": 0.0}
    events = await orchestrator.run_episode(cfg)

    # terminated cleanly on quiescence — no budget/timeout halt was needed
    assert not [e for e in events if e["type"] == "system.halt"]
    assert any(e["type"] == "agent.step" for e in events)  # it did do work first
```

Note: `quiescence_s=0.0` fires as soon as a tick sees no new event and no agent `in_step`. The seed events and the worker's one `agent.step` land first; once the loop goes idle the next tick terminates. Add `claude_code` to the imports at the top of the test file if not already present (it is, from the T10 tests).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py::test_quiescence_terminates_without_halt -v`
Expected: FAIL — currently `run_episode` only checks `_run_complete`, which never fires here, so the run hangs until the wall-clock default (300s) → test times out / does not assert clean quiescence.

- [ ] **Step 3: Replace `_run_complete` with a Terminator**

In `kernel/orchestrator.py`:

Add the import (next to `from substrate import log`):

```python
from kernel import termination
```

Delete the `_run_complete` function (lines 33-38).

Add a quiescence default near the other constants:

```python
# Backstop when no agent emits run.complete: end the run after this much idle
# time (no events, nobody mid-step, fleet not paused). T14 configs may override.
_DEFAULT_QUIESCENCE_S = 30.0
```

In `run_episode`, after `tasks = [...]` and before the rail loop, build the terminator:

```python
    term = termination.Terminator(
        run_id, agents, cfg.get("quiescence_s", _DEFAULT_QUIESCENCE_S)
    )
```

Replace the run-complete check in the loop:

```python
            if await _run_complete(run_id):  # T11 swaps in quiescence-aware terminated()
                break
```

with:

```python
            if await term.terminated():
                break
```

- [ ] **Step 4: Run the orchestrator suite**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS — the existing `test_run_complete_terminates_and_records` still passes (Terminator sees `run.complete`), plus the new quiescence test.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (all tests).

- [ ] **Step 6: Commit**

```bash
git add kernel/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(t11): orchestrator uses quiescence-aware Terminator, retire _run_complete"
```

---

### Task 4: Update the T11 doc note in the orchestrator header

**Files:**
- Modify: `kernel/orchestrator.py:1-11` (module docstring), `:29-30` and `:58-61` (T11 forward-refs now resolved).

**Interfaces:** none (docs only).

- [ ] **Step 1: Refresh the docstring references**

The module docstring says "run.complete (T11 adds quiescence)". Update it to state T11 is now in place, e.g.:

```python
run_episode(cfg): emit run.start + one seed task.created, spawn one poll loop
per role, then supervise RAILS ONLY — a budget breach becomes system.halt +
stop; run.complete OR quiescence (kernel/termination.py, T11) stops the fleet.
```

And the `_drain` comment "Surface it as an error event once the kernel has a failure channel (T11+)." — leave as-is (still a forward ref; T11 did not add a failure channel). No code change there.

- [ ] **Step 2: Run the full suite (docs change is inert)**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 3: Mark T11 done in the phase plan**

In `docs/phase1-plan.md`, mark the T11 line/section done consistently with how T10 was marked (check `git show 121210f` for the exact convention used).

- [ ] **Step 4: Commit**

```bash
git add kernel/orchestrator.py docs/phase1-plan.md
git commit -m "docs(t11): mark T11 done, resolve orchestrator forward-refs"
```

---

## Self-Review

**Spec coverage (plan §T11):**
- "Terminate on run.complete or quiescence" → Task 2 `terminated()`.
- "no new events for quiescence_s and no agent mid-step" → Task 2 quiescence branch; Task 1 `in_step`.
- "in_step flag on Agent set around await agent.step" → Task 1.
- "terminated() reads any(a.in_step for a in agents)" → Task 2.
- "cheap read_events(since_id=last_seen, limit=1) probe" → Task 2.
- "rate-limit pause is not quiescence / closed gate = busy" → Task 2 `paused` hook; Task 2 test `test_closed_resume_gate_suppresses_quiescence`.
- Self-check (1)-(4) → the five tests in Task 2. (4-case spec → 5 tests: run.complete, quiescence-fires, reset-on-event, in-step-suppress, gate-suppress.)

**Placeholder scan:** none — every code step shows full code; every command has expected output.

**Type consistency:** `Terminator(run_id, agents, quiescence_s, *, clock, paused)` and `async terminated() -> bool` are named identically in Task 2 definition, Task 2 tests, and Task 3 wiring. `Agent.in_step: bool` named identically in Task 1 def, Task 1 test, and Task 2 read.
