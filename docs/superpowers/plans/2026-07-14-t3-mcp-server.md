# T3 — `substrate/mcp_server.py` (the syscall boundary) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the T2 event log to Claude Code agents as a stdio MCP server named `substrate` with exactly two tools — `emit_event` and `read_events` — where the emitter identity (`agent`) and `run_id` are stamped server-side from env vars and can never be supplied by the client.

**Architecture:** A thin FastMCP wrapper over `substrate/log.py`. Each stateless `claude -p` step (T5) spawns a fresh stdio server process; the server learns *which agent / which run* from `AGENT_NAME` / `RUN_ID` env vars set on that subprocess (inherited by the stdio server it spawns). No shared long-running server — the monotonic-visibility invariant is carried by the advisory lock already in `log.py`, not by connection count. Self-exclusion is *not* here (that's the harness's job, §6): the MCP `read_events` returns everything, including the caller's own events.

**Tech Stack:** Python 3.12, `mcp` SDK (FastMCP, stdio transport), `asyncpg` (via `substrate.log`), pytest + pytest-asyncio.

## Global Constraints

- Python `>=3.12` (from `pyproject.toml`).
- New runtime dependency `mcp>=1.2` added to `pyproject.toml` `[project].dependencies`.
- **Server-side identity only:** `agent` and `run_id` come from env (`AGENT_NAME`, `RUN_ID`), never from tool arguments. There is no `agent` or `run_id` parameter on any tool.
- **Fail fast on missing identity:** a tool call with `AGENT_NAME`/`RUN_ID` unset must raise, never silently stamp `None`.
- **Only two tools this phase:** `emit_event`, `read_events`. No `memory_*`, `kb_query`, `write_artifact` (later phases).
- Tests require a running Postgres reachable via `DATABASE_URL` (or `localhost:5432`), matching the existing `tests/test_log.py` convention (`docker compose up db`).
- Match existing code style in `substrate/log.py`: terse, `ponytail:` comments for deliberate corner-cuts.

---

## File Structure

- `substrate/mcp_server.py` (**create**) — the FastMCP server. Module-level `mcp = FastMCP("substrate")`, two `@mcp.tool()` async functions delegating to `substrate.log`, `_require()` identity helper, `if __name__ == "__main__": mcp.run()`. The tool functions stay directly callable (FastMCP's `tool()` returns the original function) so tests exercise them without an MCP client roundtrip.
- `tests/test_mcp_server.py` (**create**) — identity-stamping + no-self-exclusion + fail-fast tests, mirroring `tests/test_log.py` fixtures.
- `pyproject.toml` (**modify**) — add `mcp>=1.2` dependency.

The MCP *client* config that tells `claude -p` to launch this server (checked-in `config/claude/`) is **T5's** responsibility (phase0-plan T5, arch §5.550) — out of scope here.

---

## Setup (fold into Task 1's first commit)

- [ ] **Create the feature branch off main**

```bash
git checkout main && git pull
git checkout -b feat/t3-mcp-server
```

---

### Task 1: Add the `mcp` dependency and install it

**Files:**
- Modify: `pyproject.toml` (the `[project].dependencies` list)

**Interfaces:**
- Consumes: nothing.
- Produces: `from mcp.server.fastmcp import FastMCP` importable in the venv.

- [ ] **Step 1: Add `mcp>=1.2` to dependencies**

In `pyproject.toml`, change:

```toml
dependencies = [
    "asyncpg>=0.29",
]
```

to:

```toml
dependencies = [
    "asyncpg>=0.29",
    "mcp>=1.2",
]
```

- [ ] **Step 2: Install into the project venv**

Run: `pip install -e '.[dev]'`
Expected: resolves and installs `mcp` (and its deps) with no errors.

- [ ] **Step 3: Verify the import works**

Run: `python -c "from mcp.server.fastmcp import FastMCP; print(FastMCP('x').name)"`
Expected: prints `x`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add mcp SDK dependency for T3 substrate server"
```

---

### Task 2: `emit_event` — server-side identity stamping

**Files:**
- Create: `substrate/mcp_server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes:
  - `substrate.log.emit(agent, type, payload, run_id, reply_to=None, correlation=None) -> {"id": int, "ts": float}`
  - `substrate.log.read_events(run_id, since_id=0, types=None, correlation=None, limit=50, exclude_agent=None) -> list[dict]`
  - `substrate.log.close()` (test teardown)
- Produces:
  - Module `substrate.mcp_server` with:
    - `mcp: FastMCP` (server name `"substrate"`)
    - `async def emit_event(type: str, payload: dict, reply_to: int | None = None, correlation: str | None = None) -> dict` — returns `{"id": int, "ts": float}`. **No `agent`/`run_id` params.**
    - `_require(name: str) -> str` — returns env var or raises `RuntimeError`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_server.py`:

```python
"""T3 check: MCP syscall boundary — server-side identity, no self-exclusion (arch §5)."""
import uuid

import pytest

from substrate import log, mcp_server


def _run_id() -> str:
    return f"test-{uuid.uuid4()}"


@pytest.fixture(autouse=True)
async def _close_pool():
    yield
    await log.close()


@pytest.fixture
def _identity(monkeypatch):
    """Set the per-session identity the harness would set on the subprocess."""
    rid = _run_id()
    monkeypatch.setenv("AGENT_NAME", "worker")
    monkeypatch.setenv("RUN_ID", rid)
    return rid


async def test_emit_event_stamps_identity_from_env(_identity):
    rid = _identity
    r = await mcp_server.emit_event("claim.made", {"answer": 42})
    assert isinstance(r["id"], int) and r["id"] > 0
    assert isinstance(r["ts"], float)
    # Identity was stamped from env, not from any argument.
    rows = await log.read_events(run_id=rid)
    assert len(rows) == 1
    assert rows[0]["agent"] == "worker"
    assert rows[0]["run_id"] == rid
    assert rows[0]["payload"] == {"v": 1, "answer": 42}


async def test_emit_event_has_no_agent_or_run_id_param():
    # The identity params must not exist on the syscall surface at all.
    import inspect

    params = set(inspect.signature(mcp_server.emit_event).parameters)
    assert "agent" not in params
    assert "run_id" not in params
    assert params == {"type", "payload", "reply_to", "correlation"}


async def test_emit_event_requires_identity(monkeypatch):
    monkeypatch.delenv("AGENT_NAME", raising=False)
    monkeypatch.setenv("RUN_ID", _run_id())
    with pytest.raises(RuntimeError):
        await mcp_server.emit_event("claim.made", {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'substrate.mcp_server'` (import error collects all three as errors).

- [ ] **Step 3: Write minimal implementation**

Create `substrate/mcp_server.py`:

```python
"""Agent-facing MCP server — the syscall boundary (arch §5).

Exposes exactly `emit_event` + `read_events` to every agent as an MCP server
named `substrate`. Stdio transport: each stateless `claude -p` step (T5)
spawns a fresh server process, so the server learns its identity from env
vars set on that subprocess — `AGENT_NAME` (the emitter, stamped server-side,
never trusted from the client) and `RUN_ID` (the session scope). Monotonic
visibility across these many short-lived writer connections is carried by the
advisory lock in log.py (T2), not by any shared server.

Self-exclusion is NOT here — that's the harness drive loop's job (§6). This
`read_events` returns everything, including the caller's own events, because
agents legitimately query their own history ("what did I already claim?").
"""
import os

from mcp.server.fastmcp import FastMCP

from substrate import log

mcp = FastMCP("substrate")


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:  # fail fast: a server with no identity must never stamp None
        raise RuntimeError(f"{name} not set — the harness must set it on the MCP subprocess")
    return v


@mcp.tool()
async def emit_event(
    type: str, payload: dict, reply_to: int | None = None, correlation: str | None = None
) -> dict:
    """Publish an event to the shared log. The only way to communicate with other agents."""
    return await log.emit(
        agent=_require("AGENT_NAME"),
        type=type,
        payload=payload,
        run_id=_require("RUN_ID"),
        reply_to=reply_to,
        correlation=correlation,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -v`
Expected: all three PASS (db must be up: `docker compose up -d db`).

- [ ] **Step 5: Commit**

```bash
git add substrate/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(t3): emit_event MCP tool with server-side identity stamping"
```

---

### Task 3: `read_events` — run-scoped, no self-exclusion

**Files:**
- Modify: `substrate/mcp_server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `substrate.log.read_events(...)` (see Task 2), `emit_event` (Task 2).
- Produces:
  - `async def read_events(since_id: int = 0, types: list[str] | None = None, correlation: str | None = None, limit: int = 50) -> list[dict]` on `substrate.mcp_server`. **No `run_id`/`exclude_agent` params** — `run_id` from env, self-exclusion never applied.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_server.py`:

```python
async def test_read_events_is_run_scoped_and_returns_own_events(_identity):
    # Two emits from the same agent; the MCP read must return BOTH — no
    # self-exclusion on the syscall surface (that's the harness's job, §6).
    await mcp_server.emit_event("claim.made", {"i": 1})
    await mcp_server.emit_event("run.complete", {"i": 2})
    rows = await mcp_server.read_events()
    assert [r["type"] for r in rows] == ["claim.made", "run.complete"]
    assert {r["agent"] for r in rows} == {"worker"}  # caller sees its own events


async def test_read_events_passes_through_filters(_identity):
    await mcp_server.emit_event("claim.made", {})
    await mcp_server.emit_event("claim.rejected", {})
    await mcp_server.emit_event("critique.made", {})
    rows = await mcp_server.read_events(types=["claim.*"])
    assert sorted(r["type"] for r in rows) == ["claim.made", "claim.rejected"]


async def test_read_events_only_sees_its_own_run(monkeypatch):
    # run_id is derived from env, not a client param — a session cannot read
    # another run's events.
    rid_a, rid_b = _run_id(), _run_id()
    monkeypatch.setenv("AGENT_NAME", "worker")
    monkeypatch.setenv("RUN_ID", rid_a)
    await mcp_server.emit_event("claim.made", {"run": "a"})
    monkeypatch.setenv("RUN_ID", rid_b)
    rows = await mcp_server.read_events()
    assert rows == []  # nothing from run a leaks into run b


async def test_read_events_has_no_run_id_or_exclude_param():
    import inspect

    params = set(inspect.signature(mcp_server.read_events).parameters)
    assert "run_id" not in params
    assert "exclude_agent" not in params
    assert params == {"since_id", "types", "correlation", "limit"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -v -k read_events`
Expected: FAIL — `AttributeError: module 'substrate.mcp_server' has no attribute 'read_events'`.

- [ ] **Step 3: Write minimal implementation**

Append to `substrate/mcp_server.py`:

```python
@mcp.tool()
async def read_events(
    since_id: int = 0,
    types: list[str] | None = None,
    correlation: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read events from the shared log, filtered. Use to observe other agents.

    Implicitly scoped to this session's run_id (server-derived). Returns the
    caller's own events too — self-exclusion lives only in the harness (§6).
    """
    return await log.read_events(
        run_id=_require("RUN_ID"),
        since_id=since_id,
        types=types,
        correlation=correlation,
        limit=limit,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -v`
Expected: all seven tests PASS.

- [ ] **Step 5: Commit**

```bash
git add substrate/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(t3): read_events MCP tool — run-scoped, no self-exclusion"
```

---

### Task 4: Stdio entry point + full-suite green

**Files:**
- Modify: `substrate/mcp_server.py`

**Interfaces:**
- Consumes: `mcp` (Task 2).
- Produces: `python -m substrate.mcp_server` runs the server over stdio (transport used by T5's `claude -p` MCP config).

- [ ] **Step 1: Add the stdio entry point**

Append to `substrate/mcp_server.py`:

```python
if __name__ == "__main__":  # stdio transport: one process per `claude -p` step (T5)
    mcp.run()
```

- [ ] **Step 2: Verify the module launches as a stdio server**

Run: `AGENT_NAME=worker RUN_ID=smoke timeout 2 python -m substrate.mcp_server; test $? -eq 124 && echo "ok: stayed up on stdio"`
Expected: prints `ok: stayed up on stdio` (the server blocks waiting for stdio input; `timeout` kills it with code 124 = it launched cleanly rather than crashing).

- [ ] **Step 3: Run the full test suite**

Run: `pytest -v`
Expected: all tests PASS (`tests/test_log.py`, `tests/test_schema.py`, `tests/test_mcp_server.py`). db must be up.

- [ ] **Step 4: Commit**

```bash
git add substrate/mcp_server.py
git commit -m "feat(t3): stdio entry point for substrate MCP server"
```

---

## After all tasks

1. **Code review:** run `/code-review` against the diff; fix findings; re-run until clean.
2. **PR:** push `feat/t3-mcp-server`, open PR against `main` describing what was built and how it was tested.
3. **Mark T3 done** in `docs/phase0-plan.md` (change the `### T3` heading to `### T3 — ... ✅ done`, matching T1/T2), and note the resolved decision: server-side identity via `AGENT_NAME`/`RUN_ID` env vars.

---

## Self-Review

**Spec coverage (phase0-plan T3 bullets):**
- "Exposes `emit_event` + `read_events` only" → Tasks 2 & 3; `memory_*`/`kb`/`artifact` explicitly excluded (Global Constraints).
- "Server-side identity stamping — never trust agent-supplied `agent`" → Task 2, `_require("AGENT_NAME")`; `test_emit_event_has_no_agent_or_run_id_param`.
- "`run_id` derived from the connection, not a client param" → Task 3, `_require("RUN_ID")`; `test_read_events_only_sees_its_own_run`, `test_read_events_has_no_run_id_or_exclude_param`.
- "Connection identity via env vars `AGENT_NAME`, `RUN_ID`" → Task 2/3 `_require`; `test_emit_event_requires_identity`.
- "Multiple server processes = multiple writer connections; invariant carried by the advisory lock in log.py, no shared server" → satisfied by delegating to `log.emit` (unchanged); documented in module docstring; no new server-side state added.
- "Self-exclusion does not live here — it's the harness's job (§6)" → Task 3, no `exclude_agent` param; `test_read_events_is_run_scoped_and_returns_own_events`.

**Placeholder scan:** none — every code and test step shows full content.

**Type consistency:** `emit_event(type, payload, reply_to, correlation) -> {"id","ts"}` and `read_events(since_id, types, correlation, limit) -> list[dict]` used identically across Interfaces blocks and test bodies; `_require(name) -> str` consistent. Delegated `log.emit`/`log.read_events` signatures match the verified `substrate/log.py`.
