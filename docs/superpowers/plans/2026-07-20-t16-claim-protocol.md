# T16 — Two-Worker Claim Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `topologies/supervisor_claim.yaml` — a supervisor plus two same-role workers with distinct identities that race to claim each `task.assigned`, where lowest-event-id wins and exactly one `claim.made` lands per task — plus the offline assertion that guards it.

**Architecture:** Two workers are expressed as **two role entries sharing one prompt via a YAML anchor** (`&claim_prompt` / `*claim_prompt`). This needs zero code: `kernel/config.py` already passes `roles` through as raw dicts, `agent.role.load_role` already builds one `Role` per entry, and `kernel/orchestrator.py` already spawns one `ClaudeCodeAgent` + poll loop per role. The claim protocol itself is model-driven and lives entirely in the shared worker prompt: emit `task.claimed` correlated to the task id, do **one** ad-hoc MCP `read_events` filtered to that correlation, and compare the id `emit_event` returned against the lowest id in the result. This is identity-free — the worker never needs to know its own name, which is exactly what lets both workers share one prompt verbatim. Correctness rests on the serialized-append invariant (`pg_advisory_xact_lock` in `substrate/log.py`): the moment `emit_event` returns id `X`, every id `< X` is already visible, so one read with zero wait is sufficient.

**Tech Stack:** Python 3.12, asyncio, PyYAML, asyncpg/Postgres event log, MCP (`substrate` server: `emit_event` / `read_events`), pytest.

## Global Constraints

- Distinct `name` per agent is **mandatory** — two agents sharing one `name` would emit `agent.step` under one identity with colliding `step_n`, corrupting per-agent projections and the replay record (phase1-plan §4). Only `name` may differ between the two workers; prompt and `subscribes_to` are shared.
- Agents coordinate **only** through the event log. No direct calls between agents.
- `correlation` is a **TEXT** column (`sql/init/01_events.sql:11`) and the MCP tool signature is `correlation: str | None` (`substrate/mcp_server.py`). Event ids are integers. Every prompt instruction and every comparison must therefore treat correlation as the **string form** of the id, and the checker must normalize with `str()`.
- Self-exclusion lives in the harness only (`agent/poll_loop.py`), never in the MCP tool. MCP `read_events` deliberately returns the caller's own events — the claim protocol depends on this.
- The claim protocol is model-driven, so the risk is model reliability. The assertion is the guard (phase1-plan §T16).
- No live-model test in the suite. The full suite must stay offline and free to run: `uv run pytest`.
- Existing suite is green at 97 tests on `main`. Do not regress it.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `kernel/claim_check.py` (create) | `assert_one_claim_per_task(events)` — the projection-level guard from §T16 "the assertion is the guard", plus a `python -m kernel.claim_check <run_id>` entry point so a live run can be verified without pytest. |
| `tests/test_claim_check.py` (create) | Unit tests for the guard against synthetic event lists: happy path, duplicate claim, missing claim, orphan correlation, string/int correlation normalization. |
| `topologies/supervisor_claim.yaml` (create) | The topology: supervisor + `worker-1` / `worker-2` sharing one prompt via YAML anchor. Encodes the claim protocol in prose for the model. |
| `tests/test_claim_topology.py` (create) | Structural + protocol-word assertions on the topology, mirroring `tests/test_supervisor_topology.py`. Guards against a prompt edit silently dropping the protocol. |
| `docs/phase1-plan.md` (modify) | Mark T16 done and record the "two role entries with a shared prompt" decision the task asked to be noted. |

---

### Task 1: The claim assertion guard

**Files:**
- Create: `kernel/claim_check.py`
- Test: `tests/test_claim_check.py`

**Interfaces:**
- Consumes: the event dict shape returned by `substrate.log.read_events` and `kernel.orchestrator.summarize` — keys `id` (int), `agent` (str), `type` (str), `payload` (dict), `correlation` (str | None).
- Produces: `assert_one_claim_per_task(events: list[dict]) -> None` — raises `AssertionError` with a descriptive message when the one-claim-per-task invariant is violated, returns `None` otherwise. Task 2 references this function by name in the topology comment and in `docs/phase1-plan.md`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_claim_check.py`:

```python
# tests/test_claim_check.py — the T16 guard: exactly one claim.made per task.assigned.
import pytest

from kernel.claim_check import assert_one_claim_per_task


def _ev(id, type, agent="worker-1", correlation=None):
    return {"id": id, "agent": agent, "type": type, "payload": {}, "correlation": correlation}


def _assigned(id):
    return _ev(id, "task.assigned", agent="supervisor")


def _claim(id, corr, agent="worker-1"):
    return _ev(id, "claim.made", agent=agent, correlation=str(corr))


def test_accepts_one_claim_per_task():
    events = [
        _ev(1, "run.start", agent="kernel"),
        _assigned(2), _assigned(3),
        _claim(10, 2, "worker-1"),
        _claim(11, 3, "worker-2"),
    ]
    assert_one_claim_per_task(events)  # must not raise


def test_rejects_duplicate_claims_for_one_task():
    events = [_assigned(2), _claim(10, 2, "worker-1"), _claim(11, 2, "worker-2")]
    with pytest.raises(AssertionError, match="2 claims"):
        assert_one_claim_per_task(events)


def test_rejects_unclaimed_task():
    events = [_assigned(2), _assigned(3), _claim(10, 2)]
    with pytest.raises(AssertionError, match="0 claims"):
        assert_one_claim_per_task(events)


def test_rejects_claim_with_no_matching_task():
    events = [_assigned(2), _claim(10, 2), _claim(11, 99)]
    with pytest.raises(AssertionError, match="no task.assigned"):
        assert_one_claim_per_task(events)


def test_normalizes_int_correlation():
    # correlation is a TEXT column, but a fake/replayed event may carry an int.
    events = [_assigned(2), {**_claim(10, 2), "correlation": 2}]
    assert_one_claim_per_task(events)  # must not raise


def test_no_tasks_is_vacuously_ok():
    assert_one_claim_per_task([_ev(1, "run.start", agent="kernel")])  # must not raise
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_claim_check.py -v`

Expected: collection error — `ModuleNotFoundError: No module named 'kernel.claim_check'`.

- [ ] **Step 3: Write the minimal implementation**

Create `kernel/claim_check.py`:

```python
"""The T16 guard: exactly one `claim.made` per `task.assigned` (phase1-plan §T16).

The claim protocol is model-driven (prompt + one ad-hoc MCP read), so the risk
is model reliability and this assertion is what catches a regression. Runs as a
projection over the event log — offline in tests against synthetic events, or
`python -m kernel.claim_check <run_id>` against a real run.
"""
from __future__ import annotations

import asyncio
import sys

from substrate import log


def assert_one_claim_per_task(events: list[dict]) -> None:
    """Raise AssertionError unless every task.assigned has exactly one claim.made
    correlated to it, and no claim.made correlates to anything else."""
    tasks = {e["id"] for e in events if e["type"] == "task.assigned"}
    claims: dict[str, list[dict]] = {}
    for e in events:
        if e["type"] == "claim.made":
            # correlation is TEXT in the log; normalize so an int-carrying
            # replayed/fake event matches the same task.
            claims.setdefault(str(e["correlation"]), []).append(e)

    for task_id in sorted(tasks):
        got = claims.get(str(task_id), [])
        assert len(got) == 1, (
            f"task.assigned id={task_id}: {len(got)} claims, expected 1 "
            f"(by {[c['agent'] for c in got]})"
        )

    orphans = sorted(set(claims) - {str(t) for t in tasks})
    assert not orphans, f"claim.made with no task.assigned for correlation(s) {orphans}"


async def _main(run_id: str) -> None:
    events = await log.read_events(run_id=run_id, limit=500)
    try:
        assert_one_claim_per_task(events)
    finally:
        await log.close()
    print(f"ok: one claim.made per task.assigned across {len(events)} events")


if __name__ == "__main__":  # verify a live run: python -m kernel.claim_check <run_id>
    asyncio.run(_main(sys.argv[1]))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_claim_check.py -v`

Expected: 6 passed.

- [ ] **Step 5: Run the full suite for regressions**

Run: `uv run pytest`

Expected: 103 passed (97 existing + 6 new), 0 failed.

- [ ] **Step 6: Commit**

```bash
git add kernel/claim_check.py tests/test_claim_check.py
git commit -m "feat(t16): assert_one_claim_per_task guard over the event log"
```

---

### Task 2: The two-worker claim topology

**Files:**
- Create: `topologies/supervisor_claim.yaml`
- Create: `tests/test_claim_topology.py`
- Modify: `docs/phase1-plan.md` (the T16 section, lines 80-84)

**Interfaces:**
- Consumes: `kernel.config.load_run_config(path) -> dict` (keys `goal`, `roles`, `usd_budget`, `run_timeout_s`, `quiescence_s`); `agent.role.load_role(dict) -> Role` (drops unknown keys); `kernel.claim_check.assert_one_claim_per_task` from Task 1 (referenced in comments/docs only, not imported by the topology).
- Produces: `topologies/supervisor_claim.yaml` with exactly three roles named `supervisor`, `worker-1`, `worker-2`. Nothing later depends on it in code — it is the runnable demo.

- [ ] **Step 1: Write the failing test**

Create `tests/test_claim_topology.py`:

```python
# tests/test_claim_topology.py — T16: two same-role workers, distinct identities.
from pathlib import Path

import yaml

from agent.role import load_role
from kernel.config import load_run_config

_TOPO = Path(__file__).resolve().parents[1] / "topologies" / "supervisor_claim.yaml"


def test_two_workers_with_distinct_names():
    cfg = load_run_config(_TOPO)
    names = [r["name"] for r in cfg["roles"]]
    assert names == ["supervisor", "worker-1", "worker-2"]
    # Distinct names are mandatory: colliding names collide step_n in the
    # agent.step record and corrupt per-agent projections (phase1-plan §4).
    assert len(set(names)) == len(names)


def test_workers_share_one_prompt_and_subscription():
    cfg = load_run_config(_TOPO)
    roles = {r["name"]: r for r in cfg["roles"]}
    w1, w2 = roles["worker-1"], roles["worker-2"]
    assert w1["prompt"] == w2["prompt"]          # shared via YAML anchor
    assert w1["subscribes_to"] == w2["subscribes_to"] == ["task.assigned"]
    assert roles["supervisor"]["subscribes_to"] == ["task.created", "claim.made"]


def test_worker_prompt_is_anchored_not_duplicated():
    # The YAML source must use an anchor/alias, so a prompt edit can never
    # drift the two workers apart into different claim logic.
    src = _TOPO.read_text()
    assert "&claim_prompt" in src and "*claim_prompt" in src


def test_worker_prompt_encodes_the_claim_protocol():
    # Model-driven claim logic: if these words go, the protocol goes with them.
    cfg = load_run_config(_TOPO)
    worker = {r["name"]: r["prompt"] for r in cfg["roles"]}["worker-1"]
    assert "task.claimed" in worker      # the claim marker
    assert "read_events" in worker       # the single ad-hoc read
    assert "correlation" in worker       # how the read is filtered
    assert "lowest" in worker.lower()    # lowest-id-wins
    assert "claim.made" in worker        # the winner's output
    assert "nothing" in worker.lower()   # the loser emits nothing


def test_roles_load_and_carry_prompts():
    cfg = load_run_config(_TOPO)
    for r in cfg["roles"]:
        role = load_role(r)          # must not raise; unknown keys dropped
        assert role.prompt.strip()


def test_topology_carries_rails():
    cfg = load_run_config(_TOPO)
    assert cfg["usd_budget"] is not None
    assert cfg["run_timeout_s"] > 0
    assert cfg["quiescence_s"] > 0


def test_no_agent_shares_a_name_with_another():
    # Guards the whole roles list, not just the workers.
    raw = yaml.safe_load(_TOPO.read_text())
    names = [r["name"] for r in raw["roles"]]
    assert len(set(names)) == len(names)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_claim_topology.py -v`

Expected: all 7 fail with `FileNotFoundError: .../topologies/supervisor_claim.yaml`.

- [ ] **Step 3: Write the topology**

Create `topologies/supervisor_claim.yaml`:

```yaml
# topologies/supervisor_claim.yaml — Phase-1 claim-protocol demo (phase1-plan §T16).
# A supervisor assigns sub-tasks; TWO workers of the same role race to claim each
# one. Lowest event id wins, the loser backs off — so exactly one `claim.made`
# lands per `task.assigned`. Verify a run with:
#   python -m kernel.claim_check <run_id>   (kernel/claim_check.py)
#
# The two workers are two role entries sharing ONE prompt via the YAML anchor
# `&claim_prompt` / `*claim_prompt`. `role.load_role` models neither duplicate
# names nor an instance count, and it does not need to: the loader passes roles
# through as raw dicts and the orchestrator spawns one agent per entry, so the
# anchor gives a shared prompt with distinct identities at zero code cost.
# Distinct names are MANDATORY — two agents under one name would emit agent.step
# with colliding step_n and corrupt per-agent projections and replay (§4).
goal: "Name three landmark public-key cryptosystems and state what hard problem each rests on."

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
      `read_events`. You never talk to the workers directly. You are STATELESS:
      each time you run you have no memory of previous steps, so you MUST
      reconstruct your state from the log every time.

      Do exactly this each time you run:

      1. If the new events include a `task.created` event, read its payload's
         `goal`. Decompose that goal into exactly 3 independent sub-questions.
         For EACH sub-question, call `emit_event` with type `task.assigned` and
         payload {"question": "<the sub-question>"}. Emit one event per
         sub-question. Do this only once — if you have already emitted
         `task.assigned` events (check step 2), do NOT emit more.

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

  - name: worker-1
    runtime: claude_code
    subscribes_to: [task.assigned]
    prompt: &claim_prompt |
      You are a WORKER in a multi-agent system. There is a SECOND worker running
      the exact same instructions at the same time, and you will both be shown
      the same `task.assigned` events. Exactly ONE of you may answer each task.
      You coordinate ONLY through the shared event log using the substrate MCP
      tools `emit_event` and `read_events`. You never talk to anyone directly.

      For EACH `task.assigned` event in the new events you were given, let X be
      that event's `id`. Run this claim protocol for X, one task at a time:

      1. CLAIM. Call `emit_event` with type `task.claimed`, payload {}, and
         correlation "<X>" (the id X as a string). The tool returns an object
         with an `id` field — call that returned id MY_CLAIM_ID.

      2. READ ONCE. Call `read_events` exactly once with types=["task.claimed"]
         and correlation="<X>" (the same string). No waiting, no retry, no second
         read: the log appends are serialized, so the moment your claim came back
         with MY_CLAIM_ID, every event with a lower id is already visible to you.
         One read is guaranteed to show every competing claim that could beat you.

      3. DECIDE. Look at the `id` of every event that read returned and take the
         LOWEST one.
         - If the lowest id equals MY_CLAIM_ID, you WON task X.
         - Otherwise you LOST task X: the other worker claimed it first.

      4a. IF YOU WON: read the sub-question in the `task.assigned` payload's
          `question` field, answer it concisely, then call `emit_event` with:
            - type: "claim.made"
            - payload: {"answer": "<your answer>"}
            - correlation: "<X>"  (the exact same string you used above)

      4b. IF YOU LOST: emit NOTHING for task X. Do not answer it, do not emit
          `claim.made`, do not emit any other event about it. Silently move on
          to the next `task.assigned` event. Losing is a normal, correct outcome.

      Never emit `claim.made` for a task you did not win. Emitting a second
      `claim.made` for a task the other worker already claimed is the one failure
      this protocol exists to prevent.

  - name: worker-2
    runtime: claude_code
    subscribes_to: [task.assigned]
    prompt: *claim_prompt
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_claim_topology.py -v`

Expected: 7 passed.

- [ ] **Step 5: Run the full suite for regressions**

Run: `uv run pytest`

Expected: 110 passed (97 existing + 6 from Task 1 + 7 new), 0 failed.

- [ ] **Step 6: Mark T16 done in the phase-1 plan**

In `docs/phase1-plan.md`, in the `### T16` section, apply two edits.

First, replace the trailing sentence of the first bullet:

```
`role.load_role` today models neither duplicate names nor an instance count — pick one (two role entries with a shared prompt, or an `instances: N` field on the role) and note it as part of this task.
```

with:

```
`role.load_role` models neither duplicate names nor an instance count — **resolved: two role entries sharing one prompt via a YAML anchor** (`&claim_prompt` / `*claim_prompt` in `topologies/supervisor_claim.yaml`). No `instances: N` field: the loader passes roles through as raw dicts and the orchestrator spawns one agent per entry, so the anchor gives a shared prompt with distinct identities at zero code cost.
```

Then replace the **Verify:** bullet:

```
- **Verify:** run the topology, assert exactly one `claim.made` per `task.assigned` across both workers. This is model-driven claim logic (prompt + MCP read), so the risk is model reliability — the assertion is the guard.
```

with:

```
- **Verify:** `kernel/claim_check.py::assert_one_claim_per_task` asserts exactly one `claim.made` per `task.assigned` across both workers, unit-tested offline in `tests/test_claim_check.py`. Against a live run: `python -m kernel.orchestrator topologies/supervisor_claim.yaml`, then `python -m kernel.claim_check <run_id>`. This is model-driven claim logic (prompt + MCP read), so the risk is model reliability — the assertion is the guard.
```

Finally, mark the T16 heading done in the same style the other completed tasks use — check how T15's heading is marked (`grep -n "T15" docs/phase1-plan.md`) and match it exactly.

- [ ] **Step 7: Commit**

```bash
git add topologies/supervisor_claim.yaml tests/test_claim_topology.py docs/phase1-plan.md
git commit -m "feat(t16): supervisor_claim topology — two workers, lowest-id-wins claim"
```

---

## Manual verification (not part of the suite)

The suite stays offline. To exercise the real thing against live models:

```bash
docker compose up -d db            # Postgres event log
python -m kernel.orchestrator topologies/supervisor_claim.yaml
# note the run_id from the printed events, then:
python -m kernel.claim_check <run_id>
```

Expected: three `task.assigned`, three `claim.made` (correlations matching the assigned ids, agents mixed across `worker-1`/`worker-2`), one `run.complete`, and `ok: one claim.made per task.assigned across N events`. Six `task.claimed` events (two per task — one per worker) are the expected, healthy footprint of the race.
