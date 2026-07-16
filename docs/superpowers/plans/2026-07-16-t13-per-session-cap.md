# T13 — Per-Session Runaway Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a per-session dollar cap onto each `claude -p` subprocess so a single runaway turn (native subagents fanning out) can't blow the global budget before the kernel reacts between steps.

**Architecture:** The T13 spec asks for `--max-turns` "(and a per-session cost cap if the CLI exposes one)". The installed `claude` CLI (v2.1.211) has **no `--max-turns` flag** — it exposes `--max-budget-usd <amount>`, a per-session dollar cap, which is exactly that alternative and serves T13's intent (bound one turn's spend) directly. So T13 adds a `max_budget_usd` field to `Role` (sane default, per-role override) and threads it onto the real `_run_claude` argv via a pure, testable `_build_argv` helper. The injected-runner test seam is untouched; the default runner binds the role's cap via `functools.partial`. The P0 single-agent path keeps working with the default.

**Tech Stack:** Python 3.13, asyncio subprocess, dataclasses, pytest.

## Global Constraints

- **CLI flag is `--max-budget-usd <amount>`, not `--max-turns`** — verified against `claude --version` 2.1.211; the turn flag does not exist in this CLI. Value passed as `str(amount)`.
- **Injected-runner seam stays `(prompt, env)`** — existing T5 tests inject fake runners with that exact signature; do not change it. The cap reaches the *default* runner via `functools.partial(_run_claude, max_budget_usd=...)`.
- **P0 must keep working** — a `Role` with no `max_budget_usd` uses the dataclass default; the single-agent demo path is unaffected.
- **`load_role` already drops unknown keys** — adding a field to `Role` is enough for config to carry it (T14's loader); no parser change.
- Flat layout; tests run via `uv run pytest` (pythonpath=. in pyproject).

---

### Task 1: Per-session `--max-budget-usd` cap from role config

**Files:**
- Modify: `agent/role.py` — add `max_budget_usd` field to `Role`
- Modify: `agent/runtimes/claude_code.py` — add `_build_argv`, thread cap into `_run_claude`, bind via `partial` in `ClaudeCodeAgent.__init__`
- Test: `tests/test_claude_code.py` — argv carries the cap; role→runner wiring; default fallback

**Interfaces:**
- Consumes: `Role` (agent/role.py), `ClaudeCodeAgent.__init__(role, run_id, runner=None)`, existing `_run_claude(prompt, env)`.
- Produces:
  - `Role.max_budget_usd: float = 1.0`
  - `_build_argv(prompt: str, max_budget_usd: float | None) -> list[str]` — the `claude -p` argv; includes `"--max-budget-usd", str(max_budget_usd)` iff `max_budget_usd is not None`.
  - `_run_claude(prompt: str, env: dict, max_budget_usd: float | None = None) -> dict` — real subprocess, argv from `_build_argv`.
  - `ClaudeCodeAgent._runner` = injected `runner` when given, else `functools.partial(_run_claude, max_budget_usd=role.max_budget_usd)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_claude_code.py`:

```python
from functools import partial

from agent.runtimes.claude_code import _build_argv, _run_claude


def test_build_argv_carries_max_budget():
    argv = _build_argv("hello", max_budget_usd=2.5)
    assert argv[:3] == ["claude", "-p", "hello"]
    assert "--max-budget-usd" in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "2.5"


def test_build_argv_omits_cap_when_none():
    argv = _build_argv("hello", max_budget_usd=None)
    assert "--max-budget-usd" not in argv
    # baseline flags still present
    assert "--output-format" in argv and "--mcp-config" in argv


def test_agent_binds_role_cap_to_default_runner():
    a = ClaudeCodeAgent(_role(), run_id="r1")  # no runner injected
    assert isinstance(a._runner, partial)
    assert a._runner.func is _run_claude
    assert a._runner.keywords["max_budget_usd"] == 1.0  # Role default


def test_agent_uses_explicit_role_cap():
    role = Role(name="worker", subscribes_to=["task.created"], max_budget_usd=0.25)
    a = ClaudeCodeAgent(role, run_id="r1")
    assert a._runner.keywords["max_budget_usd"] == 0.25


def test_injected_runner_seam_unchanged():
    async def fake(prompt, env):  # (prompt, env) signature must still work
        return {"usage": {}}
    a = ClaudeCodeAgent(_role(), run_id="r1", runner=fake)
    assert a._runner is fake
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_claude_code.py -k "argv or role_cap or seam_unchanged" -v`
Expected: FAIL — `ImportError: cannot import name '_build_argv'` (and `max_budget_usd` unknown to `Role`).

- [ ] **Step 3: Add the `max_budget_usd` field to `Role`**

In `agent/role.py`, inside the `Role` dataclass (after `tick_s`):

```python
    # ponytail: per-session runaway cap (T13). Guards ONE `claude -p` turn so a
    # subagent fanout can't blow the global usd_budget (T12) before the kernel
    # reacts between steps. Default is generous headroom for the P0 single step;
    # tune per-role via config. Raise/lower when real turn costs are known.
    max_budget_usd: float | None = 1.0
```

- [ ] **Step 4: Thread the cap through `claude_code.py`**

In `agent/runtimes/claude_code.py`:

Add `from functools import partial` near the top imports (after `import os`).

Change the runner binding in `ClaudeCodeAgent.__init__`:

```python
        self._runner = runner or partial(_run_claude, max_budget_usd=role.max_budget_usd)
```

Add the pure argv builder (place it just above `_run_claude`):

```python
def _build_argv(prompt: str, max_budget_usd: float | None) -> list[str]:
    """The `claude -p` argv. Per-session cost cap (T13) is appended when set.

    ponytail: the installed CLI (2.1.211) has no `--max-turns`; `--max-budget-usd`
    is the per-session runaway rail the T13 spec allows as the cost-cap alternative.
    Swap/extend here if a turn-count flag returns."""
    argv = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--mcp-config", str(_MCP_CONFIG),
        "--allowedTools", "mcp__substrate__emit_event,mcp__substrate__read_events",
    ]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", str(max_budget_usd)]
    return argv
```

Rewrite `_run_claude` to take the cap and use `_build_argv`:

```python
async def _run_claude(prompt: str, env: dict, max_budget_usd: float | None = None) -> dict:
    """Real subprocess. FIRST LIVE invocation happens via the orchestrator —
    tests inject a fake runner, so this path is unproven until
    kernel/orchestrator.py runs it live."""
    proc = await asyncio.create_subprocess_exec(
        *_build_argv(prompt, max_budget_usd),
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

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_claude_code.py -k "argv or role_cap or seam_unchanged" -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Run the full suite (regression)**

Run: `docker compose up -d db && uv run pytest -q`
Expected: all green — the pre-existing 73 + the new argv/cap tests. (Postgres up so the DB-backed tests pass, per prior sessions.)

- [ ] **Step 7: Mark T13 done in the phase-1 plan**

In `docs/phase1-plan.md`, change the T13 heading to `### T13 — per-session runaway cap in \`claude_code.py\` (§3.1) ✅ done` and add a one-line note that `--max-turns` is absent in CLI 2.1.211 so the per-session `--max-budget-usd` cost cap (the spec's sanctioned alternative) is the rail.

- [ ] **Step 8: Commit**

```bash
git add agent/role.py agent/runtimes/claude_code.py tests/test_claude_code.py docs/phase1-plan.md docs/superpowers/plans/2026-07-16-t13-per-session-cap.md
git commit -m "feat(t13): per-session --max-budget-usd cap on claude -p"
```

---

## Self-Review

**Spec coverage (§T13):**
- "Wire `--max-turns` (and a per-session cost cap if the CLI exposes one)" → CLI has no `--max-turns`; `--max-budget-usd` (the cost cap) is wired. Documented in Architecture + Global Constraints + Step 7. ✅
- "Sourced from the role/run config, sane default; small change to T5 runtime; P0 keeps working with the default" → `Role.max_budget_usd` default 1.0, per-role override, `partial`-bound default runner, P0 untouched. ✅
- "Self-check: against the fake-subprocess harness, assert built argv carries the flag from role config and falls back to default when unset" → `_build_argv` tests (carries value / omits when None) + role→runner wiring tests (default 1.0 / explicit 0.25). The argv seam is `_build_argv` since the injected fake runner replaces argv building entirely. ✅

**Placeholder scan:** none — all code shown in full.

**Type consistency:** `max_budget_usd: float | None` consistent across `Role`, `_build_argv`, `_run_claude`, and `partial` keyword throughout.
