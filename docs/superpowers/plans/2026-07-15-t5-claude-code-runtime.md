# T5 — Claude Code Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `agent/runtimes/claude_code.py` — a stateless per-step Claude Code runtime whose `step()` builds a prompt from new events, runs one `claude -p` subprocess, and returns usage (the model emits events via the substrate MCP directly).

**Architecture:** The log is the memory (arch §6). `ClaudeCodeAgent.step(new_events)` renders the events into a prompt, invokes `claude -p --output-format json` in a subprocess that inherits `AGENT_NAME`/`RUN_ID` (so the stdio MCP server stamps identity, T3) plus any OTel export vars (T6), and returns `([], usage)`. The subprocess call is injectable so tests use a fake runner; the **real `claude -p` path is first exercised live in T7** (`scripts/run_phase0.py`). Rate-limit failures are handled in-step with a bounded wait-and-resume retry.

**Tech Stack:** Python 3.12, asyncio subprocess, Claude Code CLI, the existing `substrate` MCP server, pytest / pytest-asyncio.

## Global Constraints

- Python `>=3.12`; deps limited to what's already declared (`asyncpg`, `mcp`); dev deps `pytest`, `pytest-asyncio`, `python-dotenv`. **No new runtime dependencies.**
- `step()` returns `(emits, usage)`; for the CC runtime `emits == []` — the model emits `claim.made`/`run.complete` through the substrate MCP, never the harness (arch §6, T4).
- Server-side identity: the harness sets `AGENT_NAME` + `RUN_ID` on the subprocess; the client never supplies the emitter (T3).
- Self-hosted Langfuse config lives in the environment (T6 owns endpoint values); T5 only forwards OTel env vars, it does not invent Langfuse specifics.
- Mark deliberate simplifications that cut a real corner with a `ponytail:` comment naming the ceiling + upgrade path.
- The live `claude -p` invocation is **deferred to T7** and MUST stay clearly marked (code comment + T7 spec note) so it is not omitted.

---

## File Structure

- Create: `agent/runtimes/__init__.py` — package marker.
- Create: `agent/runtimes/claude_code.py` — `ClaudeCodeAgent` + helpers (`_build_prompt`, `_parse_usage`, `_rate_limit_wait_s`, `_run_claude`).
- Create: `config/claude/.mcp.json` — checked-in MCP config registering the `substrate` server over stdio.
- Create: `tests/test_claude_code.py` — unit tests (fake runner; no live CLI).
- Modify: `pyproject.toml:19` — add `"agent.runtimes"` to the `packages` list so the sub-package is importable under the editable install.
- Modify: `docs/phase0-plan.md` — mark T5 done; add a T7 note that T7 is the first live `claude -p` run (validates T5's real runner).

---

## Task 1: MCP config + core step (prompt, usage, injectable runner)

**Files:**
- Create: `config/claude/.mcp.json`
- Create: `agent/runtimes/__init__.py`
- Create: `agent/runtimes/claude_code.py`
- Create: `tests/test_claude_code.py`
- Modify: `pyproject.toml:19`

**Interfaces:**
- Consumes: `agent.base.Agent` (`__init__(role, run_id)`, `step(new_events) -> tuple[list, dict]`, `.name`, `.run_id`), `agent.role.Role` (`.prompt`, `.name`, `.subscribes_to`).
- Produces:
  - `ClaudeCodeAgent(role: Role, run_id: str, runner=None)` — `runner` is `async (prompt: str, env: dict) -> dict`; defaults to the real subprocess.
  - `async ClaudeCodeAgent.step(new_events: list[dict]) -> tuple[list, dict]` → `([], usage_dict)`.
  - `_build_prompt(role_prompt: str, new_events: list[dict]) -> str`
  - `_parse_usage(result: dict) -> dict`

- [ ] **Step 1: Write the config file**

`config/claude/.mcp.json`:

```json
{
  "mcpServers": {
    "substrate": {
      "command": "python",
      "args": ["-m", "substrate.mcp_server"]
    }
  }
}
```

- [ ] **Step 2: Add the sub-package to the packages list**

`pyproject.toml` — change line 19:

```toml
packages = ["substrate", "agent", "agent.runtimes"]
```

- [ ] **Step 3: Write the failing tests**

`tests/test_claude_code.py`:

```python
"""T5 check: Claude Code runtime — prompt build, usage parse, step contract,
rate-limit wait-and-resume. No live `claude -p`; the runner is injected."""
import pytest

from agent.role import Role
from agent.runtimes.claude_code import (
    ClaudeCodeAgent,
    _build_prompt,
    _parse_usage,
)


def _role(prompt="You are a solver.", name="worker"):
    return Role(name=name, subscribes_to=["task.created"], prompt=prompt)


def test_build_prompt_includes_role_and_events():
    events = [{"id": 3, "agent": "kernel", "type": "task.created", "payload": {"goal": "answer 6*7"}}]
    p = _build_prompt("You are a solver.", events)
    assert "You are a solver." in p
    assert "task.created" in p
    assert "answer 6*7" in p
    assert "substrate" in p.lower()  # tells the model how to respond


def test_parse_usage_passthrough():
    result = {"usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.001}
    assert _parse_usage(result) == {"input_tokens": 10, "output_tokens": 5, "total_cost_usd": 0.001}


def test_parse_usage_empty():
    assert _parse_usage({}) == {}


async def test_step_returns_no_emits_and_usage():
    captured = {}

    async def fake_runner(prompt, env):
        captured["prompt"] = prompt
        captured["env"] = env
        return {"usage": {"input_tokens": 3}, "total_cost_usd": 0.0}

    a = ClaudeCodeAgent(_role(), run_id="r1", runner=fake_runner)
    events = [{"id": 1, "agent": "kernel", "type": "task.created", "payload": {"goal": "x"}}]
    emits, usage = await a.step(events)

    assert emits == []  # the model emits via the MCP, not the harness
    assert usage == {"input_tokens": 3, "total_cost_usd": 0.0}
    assert "task.created" in captured["prompt"]
    # identity is stamped on the subprocess env for the MCP server (T3)
    assert captured["env"]["AGENT_NAME"] == "worker"
    assert captured["env"]["RUN_ID"] == "r1"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/test_claude_code.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.runtimes'` (or import error for the missing symbols).

- [ ] **Step 5: Write the package marker**

`agent/runtimes/__init__.py`:

```python
"""Agent runtimes: concrete step() implementations for the shared poll loop."""
```

- [ ] **Step 6: Write the minimal implementation**

`agent/runtimes/claude_code.py`:

```python
"""Claude Code runtime (T5): stateless per-step `claude -p` subprocess.

The log is the memory (arch §6): each step renders new_events into a prompt,
runs one `claude -p` invocation, and returns usage only. The MODEL emits
claim.made / run.complete through the substrate MCP (identity stamped
server-side from AGENT_NAME/RUN_ID, T3), so step() returns emits=[] (T4).

The subprocess call is injectable (`runner`) so tests use a fake. The real
`claude -p` path (`_run_claude`) is FIRST EXERCISED LIVE IN T7
(scripts/run_phase0.py) — it is unproven until then. Do not drop the T7 live
run: it validates the CLI + MCP wiring this module only stubs in tests.
"""
import asyncio
import json
import os
from pathlib import Path

from agent.base import Agent
from agent.role import Role

# repo-root-relative so it works regardless of CWD (agent/runtimes/ → ../../).
_MCP_CONFIG = Path(__file__).resolve().parents[2] / "config" / "claude" / ".mcp.json"


class ClaudeCodeAgent(Agent):
    """Stateless per-step `claude -p`. Returns usage only; emits=[] (arch §6)."""

    def __init__(self, role: Role, run_id: str, runner=None):
        super().__init__(role, run_id)
        self.prompt = role.prompt
        self._runner = runner or _run_claude  # injectable; real subprocess by default

    async def step(self, new_events: list[dict]) -> tuple[list, dict]:
        prompt = _build_prompt(self.prompt, new_events)
        env = self._subprocess_env()
        result = await self._runner(prompt, env)
        return [], _parse_usage(result)

    def _subprocess_env(self) -> dict:
        env = dict(os.environ)  # inherit ANTHROPIC_API_KEY + any OTEL_* set by T6
        env["AGENT_NAME"] = self.name  # MCP server stamps the emitter from this (T3)
        env["RUN_ID"] = self.run_id
        return env


def _build_prompt(role_prompt: str, new_events: list[dict]) -> str:
    lines = [role_prompt.strip(), "", "New events since your last step:"]
    for e in new_events:
        lines.append(json.dumps({k: e.get(k) for k in ("id", "agent", "type", "payload")}))
    lines += ["", "React by emitting events with the substrate MCP tools."]
    return "\n".join(lines)


def _parse_usage(result: dict) -> dict:
    usage = dict(result.get("usage") or {})
    if "total_cost_usd" in result:
        usage["total_cost_usd"] = result["total_cost_usd"]
    return usage


async def _run_claude(prompt: str, env: dict) -> dict:
    """Real subprocess. FIRST LIVE invocation happens in T7 — tests inject a
    fake runner, so this path is unproven until scripts/run_phase0.py runs it."""
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        "--output-format", "json",
        "--mcp-config", str(_MCP_CONFIG),
        # non-interactive tool use: let the model call only the substrate tools
        "--allowedTools", "mcp__substrate__emit_event,mcp__substrate__read_events",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate()
    if not out:
        raise RuntimeError(
            f"claude -p produced no output (rc={proc.returncode}): {err.decode()[:500]}"
        )
    return json.loads(out)
```

- [ ] **Step 7: Reinstall so the new sub-package is importable**

Run: `pip install -e . -q`
Expected: completes without error (registers `agent.runtimes`).

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/test_claude_code.py -v`
Expected: PASS (5 tests).

- [ ] **Step 9: Commit**

```bash
git add config/claude/.mcp.json agent/runtimes/ tests/test_claude_code.py pyproject.toml
git commit -m "feat(t5): Claude Code runtime core — prompt/usage/step + MCP config"
```

---

## Task 2: Rate-limit wait-and-resume

**Files:**
- Modify: `agent/runtimes/claude_code.py`
- Modify: `tests/test_claude_code.py`

**Interfaces:**
- Consumes: `ClaudeCodeAgent` and its `_runner` from Task 1.
- Produces:
  - `_rate_limit_wait_s(result: dict, now: float) -> float | None` — seconds to wait before re-running, or `None` if not rate-limited.
  - `ClaudeCodeAgent._invoke_with_retry(prompt, env) -> dict` — wraps `_runner` with a bounded wait-and-resume loop; `step()` now calls it instead of `_runner` directly.
  - Module constants `_MAX_RETRIES`, `_MAX_WAIT_S`, `_DEFAULT_BACKOFF_S`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claude_code.py`:

```python
import time

from agent.runtimes.claude_code import _rate_limit_wait_s


def test_rate_limit_wait_s_not_error_returns_none():
    assert _rate_limit_wait_s({"usage": {}}, now=1000.0) is None


def test_rate_limit_wait_s_reset_at_epoch():
    r = {"is_error": True, "error": {"type": "rate_limit_error", "reset_at": 1060.0}}
    assert _rate_limit_wait_s(r, now=1000.0) == 60.0


def test_rate_limit_wait_s_retry_after_delta():
    r = {"is_error": True, "error": {"type": "rate_limit_error", "retry_after": 30.0}}
    assert _rate_limit_wait_s(r, now=1000.0) == 30.0


def test_rate_limit_wait_s_missing_reset_uses_backoff():
    from agent.runtimes.claude_code import _DEFAULT_BACKOFF_S
    r = {"is_error": True, "error": {"type": "rate_limit_error"}}
    assert _rate_limit_wait_s(r, now=1000.0) == _DEFAULT_BACKOFF_S


def test_rate_limit_wait_s_capped():
    from agent.runtimes.claude_code import _MAX_WAIT_S
    r = {"is_error": True, "error": {"type": "rate_limit_error", "reset_at": 10**12}}
    assert _rate_limit_wait_s(r, now=0.0) == _MAX_WAIT_S


def test_non_rate_limit_error_is_not_retried():
    r = {"is_error": True, "error": {"type": "some_other_error"}}
    assert _rate_limit_wait_s(r, now=1000.0) is None


async def test_step_retries_on_rate_limit_then_succeeds(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("agent.runtimes.claude_code.asyncio.sleep", fake_sleep)

    calls = []

    async def flaky_runner(prompt, env):
        calls.append(1)
        if len(calls) == 1:
            return {"is_error": True, "error": {"type": "rate_limit_error", "retry_after": 5.0}}
        return {"usage": {"input_tokens": 2}}

    a = ClaudeCodeAgent(_role(), run_id="r1", runner=flaky_runner)
    emits, usage = await a.step([{"id": 1, "type": "task.created", "payload": {}}])

    assert len(calls) == 2  # retried once
    assert slept == [5.0]  # waited the reset window
    assert usage == {"input_tokens": 2}


async def test_step_gives_up_after_cap(monkeypatch):
    from agent.runtimes.claude_code import _MAX_RETRIES

    async def fake_sleep(s):
        pass

    monkeypatch.setattr("agent.runtimes.claude_code.asyncio.sleep", fake_sleep)

    calls = []

    async def always_limited(prompt, env):
        calls.append(1)
        return {"is_error": True, "error": {"type": "rate_limit_error", "retry_after": 1.0}}

    a = ClaudeCodeAgent(_role(), run_id="r1", runner=always_limited)
    emits, usage = await a.step([{"id": 1, "type": "task.created", "payload": {}}])

    assert len(calls) == _MAX_RETRIES + 1  # initial try + capped retries
    assert emits == []  # still returns cleanly (usage from the limit result)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claude_code.py -k "rate_limit or retries or gives_up or non_rate" -v`
Expected: FAIL — `_rate_limit_wait_s` / `_invoke_with_retry` undefined.

- [ ] **Step 3: Write the minimal implementation**

In `agent/runtimes/claude_code.py`, add after the `_MCP_CONFIG` line:

```python
# ponytail: bounded single-agent rate-limit wait-and-resume. Fixed retry cap +
# max wait is enough for one coroutine — while it sleeps it costs zero tokens
# and other agents keep ticking (asyncio). The COORDINATED fleet-wide version
# (pause the whole fleet on a shared limit) is a Phase 1 rail (arch §9);
# upgrade path: replace this per-agent sleep with a shared limit rail.
_MAX_RETRIES = 5
_MAX_WAIT_S = 3600.0
_DEFAULT_BACKOFF_S = 60.0
```

Replace `step()` body's runner call — change:

```python
        result = await self._runner(prompt, env)
```

to:

```python
        result = await self._invoke_with_retry(prompt, env)
```

Add this method to `ClaudeCodeAgent` (after `_subprocess_env`):

```python
    async def _invoke_with_retry(self, prompt: str, env: dict) -> dict:
        import time

        for attempt in range(_MAX_RETRIES + 1):
            result = await self._runner(prompt, env)
            wait = _rate_limit_wait_s(result, time.time())
            if wait is None or attempt == _MAX_RETRIES:
                return result  # success, non-limit error, or out of retries
            await asyncio.sleep(wait)  # idle at zero token cost; cursor lives in the log
        return result  # unreachable; loop always returns inside
```

Add this module-level helper (after `_parse_usage`):

```python
def _rate_limit_wait_s(result: dict, now: float) -> float | None:
    """Seconds to wait before re-running the step, or None if not rate-limited.

    ponytail: the exact shape of a `claude -p` rate-limit result is verified
    live in T7. This tolerantly checks the likely fields (rate_limit/overloaded
    error type; reset_at epoch or retry_after delta) and falls back to a fixed
    backoff. Tighten the field names once T7 shows the real schema."""
    if not result.get("is_error"):
        return None
    err = result.get("error") or {}
    etype = err.get("type") or result.get("subtype") or ""
    if "rate_limit" not in etype and "overloaded" not in etype:
        return None
    if "retry_after" in err:
        wait = float(err["retry_after"])
    elif "reset_at" in err:
        wait = float(err["reset_at"]) - now
    else:
        wait = _DEFAULT_BACKOFF_S
    return min(max(0.0, wait), _MAX_WAIT_S)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claude_code.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add agent/runtimes/claude_code.py tests/test_claude_code.py
git commit -m "feat(t5): single-agent rate-limit wait-and-resume in step()"
```

---

## Task 3: OTel env passthrough + docs, mark live run deferred to T7

**Files:**
- Modify: `agent/runtimes/claude_code.py` (`_subprocess_env`)
- Modify: `tests/test_claude_code.py`
- Modify: `docs/phase0-plan.md`

**Interfaces:**
- Consumes: `ClaudeCodeAgent._subprocess_env` from Task 1.
- Produces: `_subprocess_env` additionally enables Claude Code telemetry when an OTLP endpoint is present in the environment (T6 supplies the endpoint value).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claude_code.py`:

```python
def test_subprocess_env_stamps_identity(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    a = ClaudeCodeAgent(_role(name="solver"), run_id="run-9")
    env = a._subprocess_env()
    assert env["AGENT_NAME"] == "solver"
    assert env["RUN_ID"] == "run-9"


def test_subprocess_env_enables_telemetry_when_otlp_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://langfuse-web:3000/api/public/otel")
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_TELEMETRY", raising=False)
    a = ClaudeCodeAgent(_role(), run_id="r1")
    env = a._subprocess_env()
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"


def test_subprocess_env_no_telemetry_without_otlp(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_TELEMETRY", raising=False)
    a = ClaudeCodeAgent(_role(), run_id="r1")
    env = a._subprocess_env()
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in env
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claude_code.py -k telemetry -v`
Expected: FAIL — telemetry is not yet enabled by `_subprocess_env`.

- [ ] **Step 3: Update `_subprocess_env`**

In `agent/runtimes/claude_code.py`, replace the `_subprocess_env` body's `return env` with:

```python
        # Claude Code's own OTel export → Langfuse OTLP gives per-model/per-tool
        # spans INSIDE the subprocess (T6 owns the endpoint/creds config in the
        # environment; T5 only turns telemetry on when an endpoint is present —
        # without this you get one span per step, not per call).
        if env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            env.setdefault("CLAUDE_CODE_ENABLE_TELEMETRY", "1")
        return env
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claude_code.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Update the phase plan — mark T5 done, flag the live run in T7**

In `docs/phase0-plan.md`, change the T5 heading:

```markdown
### T5 — `agent/runtimes/claude_code.py` ✅ done
```

And add this bullet to the **T7** section (after the "dump the `events` table" bullet):

```markdown
- **First live `claude -p` run:** T5's real subprocess runner (`_run_claude`) is only unit-tested against a fake in T5 — this demo is where it runs for real, validating the CLI + `config/claude/.mcp.json` + OTel wiring end to end. If it breaks, the fix is here, not in T5's tests.
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all existing suites + T5).

- [ ] **Step 7: Commit**

```bash
git add agent/runtimes/claude_code.py tests/test_claude_code.py docs/phase0-plan.md
git commit -m "feat(t5): OTel telemetry passthrough; mark live claude -p run deferred to T7"
```

---

## Self-Review

**Spec coverage (T5 bullets → tasks):**
- Stateless `step()` builds prompt from `new_events`, runs `claude -p` → Task 1 (`_build_prompt`, `step`, `_run_claude`).
- Uses checked-in `config/claude/` MCP config; auth via `ANTHROPIC_API_KEY` → Task 1 (`.mcp.json`; env inherits `ANTHROPIC_API_KEY`).
- Parse `usage` from result JSON → Task 1 (`_parse_usage`).
- Model emits `claim.made`/`run.complete`; `step()` returns usage only (`emits=[]`) → Task 1 (`step` returns `([], usage)`; `--allowedTools` exposes the substrate emit tool).
- Set Claude Code OTel env vars on the subprocess → Task 3 (`_subprocess_env` telemetry enable; endpoint from env per T6).
- Rate-limit wait-and-resume (single-agent, bounded, `asyncio.sleep`, `ponytail:` ceiling) → Task 2.
- Live run deferred to T7 and marked → Task 1 (code comment) + Task 3 (T7 spec note).

**Placeholder scan:** none — every code/test step shows full content.

**Type consistency:** `step -> ([], usage_dict)`, `runner: async (prompt, env) -> dict`, `_rate_limit_wait_s(result, now) -> float | None`, `_invoke_with_retry(prompt, env) -> dict` used consistently across tasks. `_role()` test helper defined in Task 1, reused in Tasks 2–3.
