# T15 — Fleet-Wide Rate-Limit Rail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift Phase 0's in-`step()` single-agent rate-limit sleep to a kernel rail that pauses the **whole fleet** on a shared resume-gate and reopens it when the limit resets.

**Architecture:** A runtime that hits a usage limit raises `RateLimitError(wait_s)` out of `step()` instead of sleeping. The poll loop catches it, closes a shared `ResumeGate` (an `asyncio.Event` every loop awaits at the top of its tick), and drops the throttled attempt whole — no `agent.step`, cursor unchanged, same event window re-delivered on resume. A single background task reopens the gate after `wait_s`. The wall-clock rail (T12 `Budget`) subtracts paused time so a throttle doesn't masquerade as a runaway; the quiescence rail (T11 `Terminator`) already treats a closed gate as "busy".

**Tech Stack:** Python 3, asyncio, pytest / pytest-asyncio. No new dependencies.

## Global Constraints

- **No new dependencies** — stdlib `asyncio` only.
- **Whole-fleet pause is the sanctioned ceiling.** Mark it with a `# ponytail:` comment naming the upgrade path (per-limit-key / per-account gates) — do not build multi-key pausing.
- **`RateLimitError` lives in `agent/base.py`** (part of the `step()` contract) so both the runtime (`agent/runtimes/`) and the poll loop (`agent/`) import it without an `agent → kernel` layering violation. `ResumeGate` lives in `kernel/rate_limit.py` (the kernel owns fleet coordination).
- **`ResumeGate` clock/sleep are injectable** (`clock=time.monotonic`, `sleep=asyncio.sleep`) so tests drive a manual clock — mirror `Budget`/`Terminator`.
- **Backward compatibility:** `run_agent`'s new `gate` parameter defaults to `None` (loop runs un-gated). Every existing `run_agent(a)` call site and test must keep passing untouched.
- **The "verify first" live check** (real `claude -p` rate-limit JSON shape) cannot be forced on demand without burning API budget to trip a real limit. The tolerant `_rate_limit_wait_s` parser and its `# ponytail:` note stay as-is; note in the T15 doc entry that the schema remains best-effort/unverified. Do **not** attempt to synthesize a live rate limit.
- **Wall-clock during a pause:** default is **exclude paused time** (fleet did no work).

---

### Task 1: `RateLimitError` contract + runtime raises it

The runtime stops sleeping on a limit; it reports up to the kernel via an exception. This retires the P0 retry-with-sleep loop.

**Files:**
- Modify: `agent/base.py` (add `RateLimitError`)
- Modify: `agent/runtimes/claude_code.py:28-35` (ponytail note), `:46-77` (`step` raises, remove `_invoke_with_retry` + `_MAX_RETRIES`)
- Test: `tests/test_claude_code.py` (replace the two retry tests; keep the `_rate_limit_wait_s` unit tests)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `agent.base.RateLimitError(wait_s: float)` — exception with a `.wait_s: float` attribute (seconds until the limit resets).
  - `ClaudeCodeAgent.step(new_events)` raises `RateLimitError(wait_s)` when `_rate_limit_wait_s(result, now)` is not `None`; otherwise returns `([], usage)` as before.

- [ ] **Step 1: Write the failing tests**

In `tests/test_claude_code.py`, **delete** `test_step_retries_on_rate_limit_then_succeeds` and `test_step_gives_up_after_cap` (the retry-with-sleep behavior is gone). Add near the other `step` tests:

```python
from agent.base import RateLimitError


async def test_step_raises_rate_limit_error(monkeypatch):
    async def limited_runner(prompt, env):
        return {"is_error": True,
                "error": {"type": "rate_limit_error", "retry_after": 42.0}}

    a = ClaudeCodeAgent(_role(), run_id="r1", runner=limited_runner)
    with pytest.raises(RateLimitError) as exc:
        await a.step([{"id": 1, "agent": "kernel", "type": "task.created", "payload": {}}])
    assert exc.value.wait_s == 42.0


async def test_step_returns_usage_when_not_limited(monkeypatch):
    async def ok_runner(prompt, env):
        return {"usage": {"input_tokens": 3}, "total_cost_usd": 0.01}

    a = ClaudeCodeAgent(_role(), run_id="r1", runner=ok_runner)
    emits, usage = await a.step([{"id": 1, "agent": "k", "type": "task.created", "payload": {}}])
    assert emits == []
    assert usage == {"input_tokens": 3, "total_cost_usd": 0.01}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_claude_code.py::test_step_raises_rate_limit_error -v`
Expected: FAIL with `ImportError: cannot import name 'RateLimitError'`.

- [ ] **Step 3: Add `RateLimitError` to `agent/base.py`**

Append to `agent/base.py` (after the `Emit` dataclass, before `class Agent`):

```python
class RateLimitError(Exception):
    """Raised by step() when the runtime hits an API/usage limit. Carries wait_s
    (seconds until the limit resets) so the poll loop pauses the whole fleet via
    the resume-gate instead of the runtime sleeping alone (phase1-plan §T15)."""

    def __init__(self, wait_s: float):
        super().__init__(f"rate limited; resume in {wait_s:.0f}s")
        self.wait_s = wait_s
```

- [ ] **Step 4: Rework `claude_code.py` — `step` raises, drop the retry loop**

In `agent/runtimes/claude_code.py`:

Add `import time` to the top imports (after `import os`).

Replace the ponytail block at lines 28-35 with:

```python
# ponytail: rate-limit handling is now a KERNEL rail (phase1-plan §T15). step()
# raises RateLimitError(wait_s) up to the poll loop, which pauses the whole fleet
# on a shared resume-gate — no per-agent sleep here. _rate_limit_wait_s stays: it
# parses the wait from the result; only the sleeping moved out.
_MAX_WAIT_S = 3600.0
_DEFAULT_BACKOFF_S = 60.0
```

(Delete `_MAX_RETRIES`. Keep `_MAX_WAIT_S` and `_DEFAULT_BACKOFF_S` — `_rate_limit_wait_s` uses them.)

Add the import to the top of the file:

```python
from agent.base import Agent, RateLimitError
```

(replacing the existing `from agent.base import Agent`).

Replace the `step` method and **delete** `_invoke_with_retry` entirely:

```python
    async def step(self, new_events: list[dict]) -> tuple[list, dict]:
        prompt = _build_prompt(self.prompt, new_events)
        env = self._subprocess_env()
        result = await self._runner(prompt, env)
        wait = _rate_limit_wait_s(result, time.time())
        if wait is not None:
            # Don't sleep alone: report up to the kernel, which pauses the whole
            # fleet and re-runs this step with the same window on resume (T15).
            raise RateLimitError(wait)
        # ponytail: non-limit errors (auth, invalid request, bad model turn) fall
        # through here and the loop advances the cursor with no run.complete — a
        # silent drop, acceptable for the demo. Surface via an error event if
        # agents must recover.
        return [], _parse_usage(result)
```

- [ ] **Step 5: Run the claude_code suite**

Run: `uv run pytest tests/test_claude_code.py -v`
Expected: PASS (new step tests green; all `_rate_limit_wait_s` unit tests still pass; no reference to the deleted retry tests).

- [ ] **Step 6: Commit**

```bash
git add agent/base.py agent/runtimes/claude_code.py tests/test_claude_code.py
git commit -m "feat(t15): step() raises RateLimitError; retire per-agent retry sleep"
```

---

### Task 2: `ResumeGate` (the shared fleet gate)

The primitive the whole rail hangs on: one gate, open by default, closed while any agent is throttled, reopened by a background task.

**Files:**
- Create: `kernel/rate_limit.py`
- Test: `tests/test_rate_limit.py`

**Interfaces:**
- Consumes: nothing.
- Produces `kernel.rate_limit.ResumeGate`:
  - `ResumeGate(*, clock=time.monotonic, sleep=asyncio.sleep)`
  - `async wait() -> None` — blocks while paused; returns immediately when open.
  - `paused` (property) `-> bool` — `True` when the gate is closed.
  - `paused_total_s() -> float` — total seconds paused, including any in-progress pause.
  - `pause(wait_s: float) -> None` — close for `wait_s` seconds (clamped to `[0, 3600]`); re-entrant — a second call while paused extends the reopen deadline to the later of the two.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rate_limit.py`:

```python
"""T15 check: ResumeGate — shared fleet resume-gate. Manual clock/sleep, no I/O
(mirrors tests/test_budget.py's Clock idiom)."""
from kernel.rate_limit import ResumeGate


class _Clock:
    """Monotonic-ish manual clock; its sleep() advances time so a gate's reopen
    loop terminates without real waiting."""
    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


def _gate(clock: _Clock) -> ResumeGate:
    return ResumeGate(clock=clock.now, sleep=clock.sleep)


async def test_gate_open_by_default():
    gate = _gate(_Clock())
    assert gate.paused is False
    await gate.wait()  # returns immediately
    assert gate.paused_total_s() == 0.0


async def test_pause_closes_then_reopens_after_wait():
    clock = _Clock()
    gate = _gate(clock)
    gate.pause(30.0)
    assert gate.paused is True
    await gate.wait()  # reopen task advances the clock, then unblocks
    assert gate.paused is False
    assert gate.paused_total_s() == 30.0


async def test_second_pause_extends_deadline():
    clock = _Clock()
    gate = _gate(clock)
    gate.pause(10.0)
    gate.pause(50.0)  # later deadline wins while already paused
    await gate.wait()
    assert gate.paused_total_s() == 50.0


async def test_pause_wait_is_clamped():
    clock = _Clock()
    gate = _gate(clock)
    gate.pause(10**9)  # absurd reset value must not hang the fleet forever
    await gate.wait()
    assert gate.paused_total_s() == 3600.0


async def test_paused_total_accrues_across_two_pauses():
    clock = _Clock()
    gate = _gate(clock)
    gate.pause(20.0)
    await gate.wait()
    gate.pause(15.0)
    await gate.wait()
    assert gate.paused_total_s() == 35.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rate_limit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kernel.rate_limit'`.

- [ ] **Step 3: Implement `kernel/rate_limit.py`**

```python
"""kernel/rate_limit.py — fleet-wide rate-limit resume-gate (phase1-plan §T15).

When a runtime hits a usage limit it raises RateLimitError(wait_s) up to its poll
loop, which closes this shared gate: every loop awaits gate.wait() at the top of
its tick, so the WHOLE FLEET pauses. A single background task reopens the gate
after wait_s. paused_total_s() lets the wall-clock rail (T12 Budget) exclude
throttled time so a reset doesn't masquerade as a runaway.

# ponytail: whole-fleet pause — one gate for every agent. Upgrade to per-limit-key
(per-account) gates only if agents ever span multiple API keys (arch §9).
"""
import asyncio
import time
from collections.abc import Awaitable, Callable

_MAX_WAIT_S = 3600.0  # never hold the fleet longer than this on a bad reset value


class ResumeGate:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self._clock = clock
        self._sleep = sleep
        self._open = asyncio.Event()
        self._open.set()  # open by default: agents proceed
        self._reopen_at = 0.0
        self._paused_since: float | None = None
        self._paused_total = 0.0
        self._reopen_task: asyncio.Task | None = None

    async def wait(self) -> None:
        """Block at the top of a tick while the fleet is paused."""
        await self._open.wait()

    @property
    def paused(self) -> bool:
        return not self._open.is_set()

    def paused_total_s(self) -> float:
        """Total seconds the fleet has spent paused, including any current pause."""
        current = (self._clock() - self._paused_since
                   if self._paused_since is not None else 0.0)
        return self._paused_total + current

    def pause(self, wait_s: float) -> None:
        """Close the gate for wait_s seconds. Re-entrant: a second limit hit while
        already paused extends the reopen deadline to the later of the two."""
        deadline = self._clock() + min(max(0.0, wait_s), _MAX_WAIT_S)
        if self.paused:
            self._reopen_at = max(self._reopen_at, deadline)  # running task re-checks
            return
        self._reopen_at = deadline
        self._paused_since = self._clock()
        self._open.clear()
        self._reopen_task = asyncio.create_task(self._reopen())

    async def _reopen(self) -> None:
        while (remaining := self._reopen_at - self._clock()) > 0:
            await self._sleep(remaining)  # deadline may extend across iterations
        self._paused_total += self._clock() - self._paused_since
        self._paused_since = None
        self._open.set()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rate_limit.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add kernel/rate_limit.py tests/test_rate_limit.py
git commit -m "feat(t15): kernel/rate_limit.py — shared fleet resume-gate"
```

---

### Task 3: Poll loop awaits the gate and drops throttled attempts

The loop consumes both new interfaces: it awaits the gate at the top of each tick and, on `RateLimitError`, pauses the fleet and re-delivers the same window without recording anything.

**Files:**
- Modify: `agent/poll_loop.py` (add `gate` param, top-of-tick `await`, `RateLimitError` catch)
- Test: `tests/test_poll_loop.py` (add one rate-limit test)

**Interfaces:**
- Consumes: `agent.base.RateLimitError` (Task 1), `kernel.rate_limit.ResumeGate` (Task 2, only in tests here).
- Produces: `run_agent(agent, gate=None, cursor=0)` — awaits `gate.wait()` before each read when `gate` is not `None`; on `RateLimitError` calls `gate.pause(e.wait_s)` and continues without advancing `cursor`, without incrementing `step_n`, and without emitting `agent.step` or forwarded events.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_poll_loop.py` (it already imports `poll_loop`, `Agent`, `Role`, `log`):

```python
from agent.base import RateLimitError
from kernel.rate_limit import ResumeGate


class _RateClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


async def test_loop_pauses_and_replays_window_on_rate_limit(monkeypatch):
    reads = []
    step_events = []  # agent.step payloads that reached the log

    async def fake_read(**kw):
        reads.append(kw)
        return [{"id": 3}, {"id": 5}]  # the SAME window every read

    async def fake_emit(agent, type, payload, run_id, reply_to=None, correlation=None):
        if type == "agent.step":
            step_events.append(payload)
        return {"id": 0, "ts": 0.0}

    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "emit", fake_emit)

    clock = _RateClock()
    gate = ResumeGate(clock=clock.now, sleep=clock.sleep)

    class _LimitThenOk(Agent):
        def __init__(self, role, run_id):
            super().__init__(role, run_id)
            self.windows = []      # the window each step() saw
            self._raised = False

        async def step(self, new_events):
            self.windows.append([e["id"] for e in new_events])
            if not self._raised:
                self._raised = True
                raise RateLimitError(30.0)
            self.stop()            # second attempt succeeds, then exit
            return [], {"ok": True}

    a = _LimitThenOk(Role(name="w", subscribes_to=["x"]), run_id="r1")
    await poll_loop.run_agent(a, gate)

    # step() ran twice on the SAME window; the throttled attempt was dropped whole.
    assert a.windows == [[3, 5], [3, 5]]
    assert a.step_n == 1                     # only the successful attempt counted
    assert len(step_events) == 1             # no agent.step for the failed attempt
    assert step_events[0]["step_n"] == 1
    assert gate.paused_total_s() == 30.0     # the fleet actually paused
    # cursor never advanced past the throttled window (second read starts at 0 too)
    assert all(r["since_id"] == 0 for r in reads)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_poll_loop.py::test_loop_pauses_and_replays_window_on_rate_limit -v`
Expected: FAIL — `run_agent()` takes 1-2 positional args but a gate was passed / `RateLimitError` propagates out of the loop.

- [ ] **Step 3: Wire the gate into `run_agent`**

Edit `agent/poll_loop.py`. Change the import line `from agent.base import Agent` to:

```python
from agent.base import Agent, RateLimitError
```

Change the signature and add the top-of-tick await + the catch. The full updated `run_agent`:

```python
async def run_agent(agent: Agent, gate=None, cursor: int = 0) -> None:
    while not agent.stopped:
        if gate is not None:
            await gate.wait()  # resume-gate: block while the fleet is throttled (T15)
        events = await log.read_events(
            run_id=agent.run_id,
            since_id=cursor,
            types=agent.subscribes_to,
            # self-exclusion lives here, never in the MCP tool (arch §5, §6)
            exclude_agent=None if agent.see_own_events else agent.name,
        )
        if not events:
            await asyncio.sleep(agent.tick_s)
            continue
        saw = [events[0]["id"], events[-1]["id"]]
        try:
            with tracing.step_span(
                agent.name, agent.run_id, agent.step_n + 1, saw, input_events=events
            ) as span:
                agent.in_step = True
                try:
                    emitted, usage = await agent.step(events)
                finally:
                    agent.in_step = False
                span.set_attributes(tracing.usage_attrs(usage))
                span.set_attributes(tracing.generation_attrs(usage))  # Tokens/Cost columns
                if span.is_recording():  # [] for the CC runtime; real emits for others
                    span.set_attribute("langfuse.observation.output", json.dumps(
                        [{"type": e.type, "payload": e.payload,
                          "reply_to": e.reply_to, "correlation": e.correlation}
                         for e in emitted], default=str))
        except RateLimitError as e:
            # Fleet throttled. Pause everyone and drop this attempt WHOLE: no
            # agent.step, step_n/cursor unchanged, so the same window is
            # re-delivered on resume (phase1-plan §T15). The coroutine stays alive
            # across the pause, so the in-place window lives in this local, not the
            # log — do not advance the cursor here.
            if gate is not None:
                gate.pause(e.wait_s)
            continue
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

- [ ] **Step 4: Run the poll-loop suite**

Run: `uv run pytest tests/test_poll_loop.py -v`
Expected: PASS (new rate-limit test green; all existing `run_agent(a)` tests still pass — `gate` defaults to `None`).

- [ ] **Step 5: Commit**

```bash
git add agent/poll_loop.py tests/test_poll_loop.py
git commit -m "feat(t15): poll loop awaits resume-gate; drops throttled attempts"
```

---

### Task 4: `Budget` excludes paused time from the wall-clock rail

A 1-hour reset must not trip a shorter `timeout_s`. The pre-factored `_elapsed()` gets a one-line subtraction.

**Files:**
- Modify: `kernel/budget.py:25-39` (`__init__` gains `paused_s`; `_elapsed` subtracts it)
- Test: `tests/test_budget.py` (add one paused-exclusion test)

**Interfaces:**
- Consumes: a `paused_s: Callable[[], float]` (in production `ResumeGate.paused_total_s`, wired in Task 5).
- Produces: `Budget(run_id, *, usd_budget=None, timeout_s=None, clock=time.monotonic, paused_s=None)` — `_elapsed()` returns `clock() - started - paused_s()`; `paused_s=None` means no paused time (`lambda: 0.0`), so existing callers are unaffected.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_budget.py` (it has `_install_log` and a `Clock` class):

```python
async def test_timeout_excludes_paused_time(monkeypatch):
    _install_log(monkeypatch)
    clock = Clock()
    paused = [0.0]
    b = budget.Budget("r", timeout_s=100.0, clock=clock,
                       paused_s=lambda: paused[0])

    clock.t = 150.0      # 150s of wall time...
    paused[0] = 60.0     # ...but 60s was a throttle -> effective 90s < 100s
    assert await b.breached() is None

    paused[0] = 40.0     # effective 110s > 100s -> the rail trips
    assert await b.breached() == "timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_budget.py::test_timeout_excludes_paused_time -v`
Expected: FAIL — `Budget.__init__() got an unexpected keyword argument 'paused_s'`.

- [ ] **Step 3: Add `paused_s` to `Budget`**

In `kernel/budget.py`, change `__init__` to accept `paused_s` and store it:

```python
    def __init__(self, run_id: str, *, usd_budget: float | None = None,
                 timeout_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 paused_s: Callable[[], float] | None = None):
        self._run_id = run_id
        self._usd_budget = usd_budget
        self._timeout_s = timeout_s
        self._clock = clock
        # T15: subtract fleet-paused seconds so a throttle isn't read as a runaway.
        self._paused_s = paused_s or (lambda: 0.0)
        self._started = clock()
        self._since_id = 0      # agent.step cursor — never re-scan
        self._spent = 0.0       # running dollar total
```

Change `_elapsed`:

```python
    def _elapsed(self) -> float:
        # Wall time minus fleet-paused time: a rate-limit pause did no work (T15).
        return self._clock() - self._started - self._paused_s()
```

- [ ] **Step 4: Run the budget suite**

Run: `uv run pytest tests/test_budget.py -v`
Expected: PASS (new test green; all existing budget tests still pass — `paused_s` defaults to zero).

- [ ] **Step 5: Commit**

```bash
git add kernel/budget.py tests/test_budget.py
git commit -m "feat(t15): Budget excludes fleet-paused time from wall-clock rail"
```

---

### Task 5: Orchestrator wiring — one gate for the fleet

Create the gate, hand it to every loop, and connect it to both existing rails.

**Files:**
- Modify: `kernel/orchestrator.py:61-92` (`run_episode`: create gate, pass to loops, wire `Terminator.paused` + `Budget.paused_s`)
- Test: `tests/test_orchestrator.py` (add one wiring test)

**Interfaces:**
- Consumes: `ResumeGate` (Task 2), `Terminator(..., paused=...)` (existing), `Budget(..., paused_s=...)` (Task 4), `run_agent(agent, gate)` (Task 3).
- Produces: no new public symbols — `run_episode` internally owns one `ResumeGate` shared by all loops and both rails.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py` (it has `_install_memory_log`):

```python
from kernel.rate_limit import ResumeGate


async def test_run_episode_shares_one_gate_across_loops_and_rails(monkeypatch):
    _install_memory_log(monkeypatch)
    captured = {"gates": [], "term_paused": None, "budget_paused_s": None}

    async def fake_run_agent(agent, gate=None, cursor=0):
        captured["gates"].append(gate)
        agent.stop()

    def fake_terminator(run_id, agents, quiescence_s, *, paused=None, **kw):
        captured["term_paused"] = paused
        class _T:
            async def terminated(self):
                return True
        return _T()

    real_budget = orchestrator.budget_mod.Budget

    def spy_budget(run_id, *, paused_s=None, **kw):
        captured["budget_paused_s"] = paused_s
        return real_budget(run_id, paused_s=paused_s, **kw)

    monkeypatch.setattr(orchestrator.poll_loop, "run_agent", fake_run_agent)
    monkeypatch.setattr(orchestrator.termination, "Terminator", fake_terminator)
    monkeypatch.setattr(orchestrator.budget_mod, "Budget", spy_budget)

    cfg = {"goal": "g",
           "roles": [{"name": "a", "subscribes_to": ["task.created"]},
                     {"name": "b", "subscribes_to": ["task.created"]}],
           "tick_s": 0.0}
    await orchestrator.run_episode(cfg)

    # Every loop got the SAME gate instance, and it's a real ResumeGate.
    gates = captured["gates"]
    assert len(gates) == 2
    assert isinstance(gates[0], ResumeGate)
    assert gates[0] is gates[1]
    # Both rails observe that same gate.
    assert captured["term_paused"]() is False        # gate open -> not paused
    assert captured["budget_paused_s"] == gates[0].paused_total_s
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py::test_run_episode_shares_one_gate_across_loops_and_rails -v`
Expected: FAIL — `run_agent` is called without a gate (captured gate is `None`) / `Terminator` gets no `paused`.

- [ ] **Step 3: Wire the gate in `run_episode`**

In `kernel/orchestrator.py`, add the import near the other kernel imports:

```python
from kernel.rate_limit import ResumeGate
```

Replace the body of `run_episode` from the `agents = ...` line through the `budget = ...` block (lines ~67-78) with:

```python
    agents = [ClaudeCodeAgent(load_role(r), run_id) for r in cfg["roles"]]
    gate = ResumeGate()  # one shared resume-gate: a limit pauses the whole fleet (T15)
    tasks = [asyncio.create_task(poll_loop.run_agent(a, gate)) for a in agents]
    term = termination.Terminator(
        run_id, agents, cfg.get("quiescence_s", _DEFAULT_QUIESCENCE_S),
        paused=lambda: gate.paused,  # a closed gate counts as busy, not quiescence
    )

    # Injectable for tests; otherwise the real rail (usd + wall-clock + kill).
    # paused_s lets the wall-clock exclude throttled time (T15).
    budget = cfg.get("budget") or budget_mod.Budget(
        run_id,
        usd_budget=cfg.get("usd_budget"),
        timeout_s=cfg.get("run_timeout_s", _DEFAULT_RUN_TIMEOUT_S),
        paused_s=gate.paused_total_s,
    )
```

- [ ] **Step 4: Run the orchestrator suite**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS (new wiring test green; all existing orchestrator tests still pass).

- [ ] **Step 5: Full suite gate**

Run: `uv run pytest -q`
Expected: PASS — all tests green (existing + T15 additions).

- [ ] **Step 6: Commit**

```bash
git add kernel/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(t15): orchestrator shares one resume-gate across loops + rails"
```

---

### Task 6: Mark T15 done in the phase-1 plan

**Files:**
- Modify: `docs/phase1-plan.md:71-77` (T15 section)

- [ ] **Step 1: Update the T15 entry**

In `docs/phase1-plan.md`, change the heading `### T15 — fleet-wide rate-limit rail` to `### T15 — fleet-wide rate-limit rail ✅ done` and prepend a **Landed** bullet under it summarizing what shipped:

```markdown
- **Landed:** runtimes raise `agent.base.RateLimitError(wait_s)` out of `step()` (per-agent retry-sleep retired from `claude_code.py`); `kernel/rate_limit.py` `ResumeGate` is a shared `asyncio.Event` every poll loop awaits at tick-top, closed by `run_agent`'s `RateLimitError` catch (which drops the attempt whole — no `agent.step`, cursor/`step_n` unchanged, same window re-delivered on resume) and reopened by one background task at the deadline; `Budget` subtracts `gate.paused_total_s()` and `Terminator` reads `gate.paused` as busy. `run_episode` owns one gate for the whole fleet. **Ceiling:** whole-fleet pause (`# ponytail:` in `kernel/rate_limit.py`) — upgrade to per-limit-key gates if agents span multiple API keys. **Verify-first caveat:** a live `claude -p` rate-limit could not be forced without burning budget, so `_rate_limit_wait_s`'s field names stay best-effort/unverified (its `# ponytail:` note stands).
```

- [ ] **Step 2: Commit**

```bash
git add docs/phase1-plan.md
git commit -m "docs(t15): mark T15 done in phase1-plan"
```

---

## Self-Review

**Spec coverage** (phase1-plan §T15):
- "Lift P0's in-`step()` wait to a kernel rail; runtime reports `reset_at`/wait up instead of sleeping" → Task 1 (`step` raises `RateLimitError`, retry-sleep removed).
- "shared `asyncio.Event` resume-gate each loop awaits at the top of its tick; reopens at reset" → Task 2 (`ResumeGate`) + Task 3 (poll loop awaits).
- "`step()` raises `RateLimitError`; **remove the internal sleep in `_invoke_with_retry`**" → Task 1 (`_invoke_with_retry` deleted).
- "poll loop catches it, does **not** advance the cursor, does **not** emit `agent.step`; re-runs with the same window" → Task 3 (catch → `continue`; test asserts same window twice, one `agent.step`, `since_id` unchanged).
- "cursor is a local in `run_agent`; coroutine stays alive across the pause" → Task 3 comment + test (cursor never advances).
- "wall-clock excludes paused time (T12 interaction)" → Task 4 (`Budget.paused_s`).
- "`# ponytail:` whole-fleet pause, upgrade to per-limit-key" → Task 2 (module docstring ponytail note).
- "Self-check: fake runtime raising `RateLimitError` closes the gate for all loops; no `agent.step` for the failed attempt; cursor unchanged; gate reopens; same window re-delivered" → Task 3 test (loop-level) + Task 5 test (shared-gate wiring across loops).
- "Verify first: real rate-limit JSON shape" → Global Constraints + Task 6 caveat (cannot force live; parser stays best-effort).
- Whole-fleet pause wired to both rails → Task 5.

**Placeholder scan:** none — every code step shows complete code; every run step shows the command and expected result.

**Type consistency:** `RateLimitError(wait_s: float)` / `.wait_s` used identically in Tasks 1, 3. `ResumeGate.pause(wait_s)`, `.paused` (property), `.paused_total_s()`, `.wait()` used identically in Tasks 2, 3, 5. `Budget(..., paused_s=Callable)` matches between Tasks 4 and 5. `run_agent(agent, gate=None, cursor=0)` matches between Tasks 3 and 5.
