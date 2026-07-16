# T14 — `topologies/supervisor.yaml` + run-config loader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the orchestrator's hardcoded `_stub_cfg()` with a YAML topology file (`topologies/supervisor.yaml`) loaded by a small config loader, and express the Phase-1 supervisor→worker decompose/delegate demo as pure data (roles + prompts + rails) so the first real two-agent run is driven entirely by config.

**Architecture:** A new `kernel/config.py` exposes `load_run_config(path) -> dict` that reads a YAML file and returns exactly the `cfg` dict `run_episode` already consumes (`goal`, `roles`, plus rails: `usd_budget`, `run_timeout_s`, `quiescence_s`, optional `tick_s`). Roles stay untouched dicts passed straight to the existing `role.load_role` (which already drops unknown keys — so `runtime`/`model` fields are inert in Phase 1, no runtime dispatch is added). `topologies/supervisor.yaml` carries the goal, the two roles (supervisor + worker), and their prompts — including the stateless-aggregation instructions that make the supervisor read its own prior `task.assigned`/`claim.made` history via the substrate MCP each step. `orchestrator.main()` loads the topology instead of building a stub.

**Tech Stack:** Python 3, `PyYAML` (new dependency — no YAML parser in stdlib), existing `asyncio`/MCP substrate, `pytest`.

## Global Constraints

- **Loader output shape is fixed by `run_episode`** (`kernel/orchestrator.py:58-76`): it reads `cfg["goal"]`, `cfg["roles"]` (list of dicts for `load_role`), and optional `cfg.get("quiescence_s")`, `cfg.get("usd_budget")`, `cfg.get("run_timeout_s")`, `cfg.get("tick_s")`, `cfg.get("budget")`. The loader MUST emit these exact keys — do not rename them and do not touch `run_episode`'s reads.
- **No new parser for roles.** Roles are passed as raw dicts to `agent.role.load_role`, which drops keys `Role` doesn't model. Do not add role validation here.
- **No runtime dispatch.** Phase 1 uses `ClaudeCodeAgent` for every role (locked decision, phase1-plan §"Both runtimes"). The `runtime`/`model` YAML fields are documentation-only in Phase 1 and are dropped by `load_role`. Adding a runtime factory is Phase 2 — YAGNI.
- **Pure-log demo.** No artifacts, no git, no direct agent-to-agent calls. Coordination is only through the event log.
- **The substrate acceptance test (phase1-plan §T14 / §7):** expressing this topology must touch **only** YAML + prompts + the loader — never the kernel or a schema. If a task here forces a `run_episode`/`Role` change to express the topology, stop and flag it.
- Test invocation: `uv run pytest <path> -v` (repo has `pythonpath = "."` in `pyproject.toml`).

---

### Task 1: `kernel/config.py` — YAML run-config loader

**Files:**
- Create: `kernel/config.py`
- Create: `tests/test_config.py`
- Modify: `pyproject.toml` (add `pyyaml` to `dependencies`)

**Interfaces:**
- Consumes: nothing from other tasks. Reads the `cfg` shape documented in `kernel/orchestrator.py:58-76`.
- Produces: `load_run_config(path: str | Path) -> dict` returning a dict with keys `goal: str`, `roles: list[dict]`, and any of `usd_budget: float | None`, `run_timeout_s: float`, `quiescence_s: float`, `tick_s: float` that were present under the YAML `rails:` block. A YAML `rails.timeout_s` maps to the output key `run_timeout_s` (the name `run_episode` reads). Rails keys absent from the YAML are omitted from the output dict (so `run_episode`'s own defaults apply).

- [ ] **Step 1: Add the PyYAML dependency**

In `pyproject.toml`, add `"pyyaml>=6"` to the `[project]` `dependencies` list (alongside the existing deps). Then sync:

```bash
uv sync
uv run python -c "import yaml; print(yaml.__version__)"
```
Expected: prints a version like `6.0.x` (currently `import yaml` fails — this fixes it).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_config.py
from pathlib import Path

from kernel.config import load_run_config

_YAML = """\
goal: "What is the capital of France?"
rails:
  usd_budget: 5.0
  timeout_s: 120
  quiescence_s: 20
roles:
  - name: supervisor
    subscribes_to: [task.created, claim.made]
    prompt: "decompose then aggregate"
  - name: worker
    subscribes_to: [task.assigned]
    prompt: "answer each sub-question"
    runtime: claude_code   # inert in Phase 1 — load_role drops it
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "topo.yaml"
    p.write_text(text)
    return p


def test_loads_goal_roles_and_maps_rails(tmp_path):
    cfg = load_run_config(_write(tmp_path, _YAML))
    assert cfg["goal"] == "What is the capital of France?"
    assert [r["name"] for r in cfg["roles"]] == ["supervisor", "worker"]
    assert cfg["roles"][0]["subscribes_to"] == ["task.created", "claim.made"]
    # rails: timeout_s -> run_timeout_s, others pass through under their own names
    assert cfg["usd_budget"] == 5.0
    assert cfg["run_timeout_s"] == 120
    assert cfg["quiescence_s"] == 20
    assert "timeout_s" not in cfg  # renamed, not duplicated


def test_absent_rails_are_omitted(tmp_path):
    text = 'goal: "g"\nroles:\n  - name: w\n    subscribes_to: [task.created]\n    prompt: "p"\n'
    cfg = load_run_config(_write(tmp_path, text))
    assert cfg["goal"] == "g"
    assert cfg["roles"][0]["name"] == "w"
    # no rails block -> run_episode's own defaults apply, so keys must be absent
    for k in ("usd_budget", "run_timeout_s", "quiescence_s", "tick_s"):
        assert k not in cfg


def test_missing_goal_raises(tmp_path):
    text = 'roles:\n  - name: w\n    subscribes_to: [task.created]\n    prompt: "p"\n'
    import pytest
    with pytest.raises(KeyError):
        load_run_config(_write(tmp_path, text))
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kernel.config'`.

- [ ] **Step 4: Write the minimal loader**

```python
# kernel/config.py
"""Run-config loader (phase1-plan §T14): YAML topology -> the cfg dict
run_episode consumes (kernel/orchestrator.py). Roles stay raw dicts for
agent.role.load_role (which drops unknown keys), so this is a thin mapper,
not a schema — the substrate acceptance test (§7) requires that expressing a
topology touches only YAML + prompts + this loader.
"""
from __future__ import annotations

from pathlib import Path

import yaml

# YAML rails.<key> -> cfg key run_episode reads. Only timeout_s is renamed
# (run_episode calls it run_timeout_s); the rest pass through unchanged.
_RAILS_KEYS = {
    "usd_budget": "usd_budget",
    "timeout_s": "run_timeout_s",
    "quiescence_s": "quiescence_s",
    "tick_s": "tick_s",
}


def load_run_config(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    cfg: dict = {
        "goal": data["goal"],          # KeyError if absent — a run needs a seed task
        "roles": data["roles"],        # raw dicts; load_role drops unknown keys
    }
    for yaml_key, cfg_key in _RAILS_KEYS.items():
        if yaml_key in (data.get("rails") or {}):
            cfg[cfg_key] = data["rails"][yaml_key]
    return cfg
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock kernel/config.py tests/test_config.py
git commit -m "feat(t14): kernel/config.py — YAML run-config loader"
```

---

### Task 2: `topologies/supervisor.yaml` — the decompose/delegate topology

**Files:**
- Create: `topologies/supervisor.yaml`
- Create: `tests/test_supervisor_topology.py`

**Interfaces:**
- Consumes: `kernel.config.load_run_config` (Task 1).
- Produces: `topologies/supervisor.yaml` — a topology whose loaded cfg has a `supervisor` role (`subscribes_to: [task.created, claim.made]`) and a `worker` role (`subscribes_to: [task.assigned]`), both loadable by `agent.role.load_role`, plus rails. This file is loaded by `orchestrator.main()` in Task 3.

**Design notes (the hard part — phase1-plan §T14):** the supervisor is stateless. Each `step()` is a fresh `claude -p` with no memory of the sub-questions it emitted last step. So the prompt must, every step, use the substrate MCP `read_events` tool to reconstruct state from the log: read its own prior `task.assigned` events and the `claim.made` events, match them by `correlation`, and only emit `run.complete` once every sub-question has an answer. The worker must set `correlation` on its `claim.made` to the `id` of the `task.assigned` it is answering (that `id` is rendered into the prompt by `_build_prompt`). This correlation key is what lets the supervisor's ad-hoc read pair questions with answers. If the prompt doesn't do this, the run never terminates cleanly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_supervisor_topology.py
from pathlib import Path

from agent.role import load_role
from kernel.config import load_run_config

_TOPO = Path(__file__).resolve().parents[1] / "topologies" / "supervisor.yaml"


def test_topology_has_supervisor_and_worker():
    cfg = load_run_config(_TOPO)
    roles = {r["name"]: r for r in cfg["roles"]}
    assert set(roles) == {"supervisor", "worker"}
    assert roles["supervisor"]["subscribes_to"] == ["task.created", "claim.made"]
    assert roles["worker"]["subscribes_to"] == ["task.assigned"]


def test_roles_load_and_carry_prompts():
    cfg = load_run_config(_TOPO)
    for r in cfg["roles"]:
        role = load_role(r)          # must not raise; unknown keys dropped
        assert role.prompt.strip()   # every role has a non-empty prompt


def test_prompts_encode_the_correlation_protocol():
    # The stateless-aggregation contract must be spelled out in the prompts,
    # or the live run never terminates (phase1-plan §T14). Assert the key
    # protocol words are present so a prompt edit that drops them is caught.
    cfg = load_run_config(_TOPO)
    roles = {r["name"]: r["prompt"] for r in cfg["roles"]}
    sup, worker = roles["supervisor"], roles["worker"]
    assert "task.assigned" in sup and "claim.made" in sup
    assert "read_events" in sup and "correlation" in sup
    assert "run.complete" in sup
    assert "task.assigned" in worker and "claim.made" in worker
    assert "correlation" in worker


def test_topology_carries_rails():
    cfg = load_run_config(_TOPO)
    assert cfg["usd_budget"] is not None
    assert cfg["run_timeout_s"] > 0
    assert cfg["quiescence_s"] > 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_supervisor_topology.py -v`
Expected: FAIL — `FileNotFoundError` on `topologies/supervisor.yaml`.

- [ ] **Step 3: Write the topology file**

```yaml
# topologies/supervisor.yaml — Phase-1 decompose/delegate demo (phase1-plan §T14).
# Pure-log: a supervisor splits a goal into sub-questions, a worker answers each,
# the supervisor aggregates and ends the run. Coordination is ONLY through the
# event log. Both roles run on ClaudeCodeAgent (Phase-1 locked decision); the
# `runtime` field is documentation here and is dropped by load_role.
goal: "What are the three primary colors, and what does mixing all three produce?"

rails:
  usd_budget: 5.0      # global cost cap (T12); per-turn cap is Role.max_budget_usd (T13)
  timeout_s: 300       # wall-clock backstop -> run_timeout_s
  quiescence_s: 30     # end the run after this much idle time if no run.complete

roles:
  - name: supervisor
    runtime: claude_code
    subscribes_to: [task.created, claim.made]
    prompt: |
      You are the SUPERVISOR in a multi-agent system. You coordinate ONLY through
      the shared event log using the substrate MCP tools `emit_event` and
      `read_events`. You never talk to the worker directly. You are STATELESS:
      each time you run you have no memory of previous steps, so you MUST
      reconstruct your state from the log every time.

      Do exactly this each time you run:

      1. If the new events include a `task.created` event, read its payload's
         `goal`. Decompose that goal into 2-3 independent sub-questions. For EACH
         sub-question, call `emit_event` with type `task.assigned` and payload
         {"question": "<the sub-question>"}. Emit one event per sub-question.
         Do this only once — if you have already emitted `task.assigned` events
         (check step 2), do NOT emit more.

      2. Reconstruct progress from the log. Call `read_events` with
         types=["task.assigned"] to get every sub-question you assigned, and
         `read_events` with types=["claim.made"] to get every answer so far.
         Each `claim.made` carries a `correlation` equal to the `id` of the
         `task.assigned` it answers. Match answers to questions by that
         `correlation` value.

      3. If EVERY `task.assigned` id has a matching `claim.made` (by
         correlation), then aggregate the answers into one final answer and call
         `emit_event` with type `run.complete` and payload
         {"answer": "<aggregated final answer>"}. If some sub-questions are still
         unanswered, emit nothing and wait for the next step.

      Emit `run.complete` exactly once, only when all sub-questions are answered.

  - name: worker
    runtime: claude_code
    subscribes_to: [task.assigned]
    prompt: |
      You are a WORKER in a multi-agent system. You coordinate ONLY through the
      shared event log using the substrate MCP tool `emit_event`. You never talk
      to the supervisor directly.

      For each `task.assigned` event in the new events you were given: read the
      sub-question in its payload's `question` field, answer it concisely, then
      call `emit_event` with:
        - type: "claim.made"
        - payload: {"answer": "<your answer>"}
        - correlation: <the `id` of the `task.assigned` event you are answering>

      The `correlation` MUST be the exact `id` of the `task.assigned` event shown
      to you — this is how the supervisor pairs your answer with its question.
      Emit one `claim.made` per `task.assigned`. Do not emit anything else.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_supervisor_topology.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add topologies/supervisor.yaml tests/test_supervisor_topology.py
git commit -m "feat(t14): topologies/supervisor.yaml — decompose/delegate topology"
```

---

### Task 3: Wire the orchestrator to the topology; retire `_stub_cfg`

**Files:**
- Modify: `kernel/orchestrator.py:92-127` (retire `_stub_cfg`/`_STUB_*`/`_WORKER_PROMPT`, load YAML in `main`)
- Modify: `tests/test_orchestrator.py` (add a test that `main` loads the topology cfg)
- Modify: `docker-compose.yml` (pass the topology path to the `kernel` command) — **only if** the command needs the arg; see Step 1.

**Interfaces:**
- Consumes: `kernel.config.load_run_config` (Task 1), `topologies/supervisor.yaml` (Task 2).
- Produces: `main()` loads the topology into `cfg` and calls `run_episode(cfg)`. A module constant `_DEFAULT_TOPOLOGY: Path` points at `topologies/supervisor.yaml`; the path may be overridden by `argv[1]` or the `TOPOLOGY` env var (absorbing the "run-config path via arg/env" note from phase1-plan §T10).

- [ ] **Step 1: Inspect the docker-compose kernel command**

Run: `grep -nA6 "kernel" docker-compose.yml`
If the `kernel` service `command` is `python -m kernel.orchestrator` with no config arg, leave it — Step 3 makes the topology path default, so no compose change is required. Only add a config-path arg to the command if compose already passed one to `run_phase0`. (Record what you found; the plan assumes the default-path case and skips the compose edit. If an arg is needed, add `command: python -m kernel.orchestrator topologies/supervisor.yaml`.)

- [ ] **Step 2: Write the failing test**

Add to `tests/test_orchestrator.py`:

```python
def test_main_loads_supervisor_topology(monkeypatch):
    """main() must build its cfg from the YAML topology, not a hardcoded stub."""
    import kernel.orchestrator as orch

    captured = {}

    async def fake_run_episode(cfg, **kw):
        captured["cfg"] = cfg
        return []

    monkeypatch.setattr(orch, "run_episode", fake_run_episode)
    monkeypatch.setattr(orch.tracing, "configure_tracing", lambda: None)
    monkeypatch.setattr(orch.tracing, "shutdown_tracing", lambda: None)

    import asyncio
    asyncio.run(orch.main())

    cfg = captured["cfg"]
    names = {r["name"] for r in cfg["roles"]}
    assert names == {"supervisor", "worker"}
    assert cfg["goal"]  # seeded from the topology, not empty
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py::test_main_loads_supervisor_topology -v`
Expected: FAIL — `main` still calls `_stub_cfg()` whose single role is `worker` (assert on role names fails), or `main` references removed symbols.

- [ ] **Step 4: Rewrite the `main` / stub section of `kernel/orchestrator.py`**

Replace the block from the `# --- docker kernel command` comment (line ~92) through the end of `main()` with:

```python
# --- docker `kernel` command: load the YAML topology (phase1-plan §T14) --------

from pathlib import Path  # noqa: E402  (kept local to the entry-point section)

_DEFAULT_TOPOLOGY = Path(__file__).resolve().parent.parent / "topologies" / "supervisor.yaml"


def _topology_path() -> Path:
    """Run-config path via arg or env, else the default topology (phase1-plan
    §T10: 'run-config path via arg/env')."""
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    return Path(os.environ.get("TOPOLOGY", _DEFAULT_TOPOLOGY))


async def main() -> None:
    tracing.configure_tracing()  # first live OTLP export call site (absorbs run_phase0)
    cfg = config.load_run_config(_topology_path())
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

Then update the imports at the top of `kernel/orchestrator.py`: add `import os`, `import sys`, and `from kernel import config`. Remove the now-unused `_STUB_GOAL`, `_WORKER_PROMPT`, and `_stub_cfg` definitions (the whole block Step 4 replaces). Keep the docstring reference accurate — the module no longer carries a stub.

- [ ] **Step 5: Run the new test and the full orchestrator suite**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS — the new test passes and no existing orchestrator test regressed.

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest -v`
Expected: all tests pass (start Postgres via `docker compose up -d db` first if the DB-backed tests need it — per prior runs, 67/67 green with Postgres up).

- [ ] **Step 7: Commit**

```bash
git add kernel/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(t14): orchestrator loads topologies/supervisor.yaml; retire _stub_cfg"
```

---

### Task 4: Mark T14 done in the phase plan

**Files:**
- Modify: `docs/phase1-plan.md:64` (mark T14 ✅ done, matching the T10–T13 convention)

**Interfaces:** none.

- [ ] **Step 1: Update the task heading**

In `docs/phase1-plan.md`, change the `### T14 — topologies/supervisor.yaml + run-config loader` heading to append ` ✅ done` (matching T10–T13). Add a one-line note under it if any decision diverged from the spec (e.g. rails key mapping `timeout_s` → `run_timeout_s`, or the PyYAML dependency).

- [ ] **Step 2: Commit**

```bash
git add docs/phase1-plan.md
git commit -m "docs(t14): mark T14 done in phase1-plan"
```

---

## Notes for the live run (not a task — validated at `docker compose up`)

The supervisor's stateless aggregation is prompt-driven and cannot be unit-tested without a live model. Its acceptance test is the live orchestrator run (phase1-plan exit criterion): `docker compose up` → supervisor decomposes → worker answers each → supervisor aggregates → `run.complete`, with every step in the `events` table. If the run hangs, the failure is almost always the correlation protocol in the prompts (worker not setting `correlation`, or supervisor not matching by it) — fix in the YAML prompts only, never the kernel (that's the §7 substrate acceptance test).
