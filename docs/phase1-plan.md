# Phase 1 — Two Agents, One Topology

**Status:** Plan v0.1
**Date:** 2026-07-15
**Parent:** [agentic-os-architecture.md](./agentic-os-architecture.md) §9
**Predecessor:** [phase0-plan.md](./phase0-plan.md) (T1–T8, merged; T9 span-enrichment lands with `feat/t9-enrich-step-spans`)
**Goal:** `docker compose up` → `kernel/orchestrator.py` loads `topologies/supervisor.yaml` → a **supervisor** and a **worker** (both Claude Code) solve one task **coordinating only through the log**, under kernel-enforced rails, and the episode is *recorded* replayably — every step visible in Langfuse and the `events` table.

---

## Locked decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Entry point | **Orchestrator replaces `run_phase0.py`** | `run_episode(cfg)` (§6) is the real kernel the throwaway P0 runner stood in for. `docker-compose` command → `python -m kernel.orchestrator`; `run_phase0.py` is retired. |
| Dispatch | **`poll_loop.run_agent` dispatch/routing semantics unchanged** | The kernel spawns one loop per role and supervises rails; it never inspects event *content*. Routing stays purely the `types` filter (§6) — that invariant keeps experiments clean. T11 and T15 add small rail *hooks* to the loop (an `in_step` flag; a resume-gate await + `RateLimitError` catch) but change nothing about how it reads, self-excludes, or emits. |
| Demo task | **Pure-log decompose + delegate** | Supervisor splits a goal into 2–3 sub-questions (`task.assigned` fan-out), worker answers each (`claim.made`), supervisor aggregates → `run.complete`. No artifact/git dependency (that's Phase 2); exercises only the Phase 1 surface (rails + topology + claim). |
| Both runtimes | **`ClaudeCodeAgent` (T5) for both roles** | Truest to "two agents"; validates topology-as-config with **zero new runtime code**. Slower/costlier demo (two live `claude -p` loops) is the accepted trade. Heterogeneous runtimes are Phase 2. |
| Rate-limit | **Lift P0's in-`step()` wait to a kernel rail** | Phase 0's single-agent wait-and-resume becomes coordination policy: a kernel-owned shared gate pauses the fleet and resumes it together on reset. It belongs in the rails, not the substrate. |

---

## Scope boundary

**In:** `kernel/orchestrator.py` (spawn loops + rails + termination), `kernel/termination.py` (quiescence excluding mid-step agents / `run.complete`), `kernel/budget.py` (`usd_budget` + wall-clock + kill switch), a per-session `max_turns` rail inside `claude_code.py`, `topologies/supervisor.yaml` + a run-config loader, the fleet-wide rate-limit rail, and the two-worker claim-protocol demo.

**Deferred:** second runtime / `llm.py` / `FunctionAgent` / Gmail summarizer, human-in-the-loop, KB + ingestion, artifacts/`write_artifact` (all Phase 2); playback engine + original→replay id-map + `test_replay.py` (Phase 3). Phase 1 only *records* enough to replay later.

---

## Task list

### T10 — `kernel/orchestrator.py` (the dumb kernel — risk center) ✅ done
- `run_episode(cfg)` (§6): emit `run.start` + one seed `task.created`, build one `ClaudeCodeAgent` per role from the config, `asyncio.create_task(run_agent(a))` per agent, then run the supervise loop.
- **Supervise rails, nothing more (§3.1):** each tick check `budget.breached()` (T12); on breach emit `system.halt` and `a.stop()` every agent. The kernel does **not** decide who works next, summarize, or route by content — routing is purely the `types` filter carried by each loop.
- **Detect termination (T11):** `run.complete` **or** quiescence; on either, stop all agents and break.
- Return a projection over the event log (`summarize(run_id)`) — the same "dump the events table" the P0 runner did, now the kernel's job.
- **Absorbs `run_phase0.py`:** becomes the `docker-compose` `kernel` command (`python -m kernel.orchestrator`, run-config path via arg/env). Reuse P0's drain-before-teardown discipline so the final in-flight `agent.step` is recorded before shutdown.
- **Config dependency (T10 lands before T14's loader):** T10 builds agents from a hardcoded stub `cfg` dict (roles + seed + rails inline, mirroring `run_phase0.py`); T14 replaces the stub with the YAML loader. So T10 defines the `cfg` *shape* it consumes; T14 supplies the parser.
- **Self-check:** with two fake agents (no model) and a stub cfg, `run_episode` spawns both loops, a seeded `run.complete` terminates the run, and `system.halt` fires when a forced budget breach is injected — assert both agents stopped and the halt event is in the log.

### T11 — `kernel/termination.py` (quiescence — risk center) ✅ done
- Terminate on **`run.complete`** (any agent/kernel emits it) **or quiescence**: no new events for `quiescence_s` **and no agent currently mid-step** (§3.1). Excluding in-flight agents is the whole point — a Claude Code agent can be minutes into a tool-using turn without emitting; naive "no events for T seconds" fires mid-thought and truncates the run.
- **The kernel owns the sessions, so it knows who is stepping.** Add a small `in_step` flag on `Agent` (`base.py`) set around the `await agent.step(...)` call in `poll_loop.py`; `terminated()` reads `any(a.in_step for a in agents)`. This is the only change to the P0 loop, and it's inert for P0's single-agent path.
- Quiescence needs a "last event id / time" probe over the run — one cheap `read_events(since_id=last_seen, limit=1)` per tick, not a full scan.
- **A rate-limit pause is not quiescence (interaction with T15):** agents blocked at T15's resume-gate are *not* `in_step` and emit nothing, so naive quiescence would kill a run that is merely throttled. `terminated()` must also treat **the gate being closed** as "busy" — i.e. no quiescence while the fleet is paused. (In P0 this couldn't happen because the wait lived *inside* `step()`; T15's lift to a kernel gate is what introduces it.)
- **Self-check:** with fake agents and a fake clock, assert (1) `run.complete` terminates immediately; (2) quiescence fires only after `quiescence_s` of no events *and* no agent `in_step`; (3) an `in_step` agent suppresses quiescence past the threshold; (4) a closed resume-gate suppresses quiescence.

### T12 — `kernel/budget.py` (the rails) ✅ done
- Global **`usd_budget`** summed from `agent.step` `usage` (§4) + **wall-clock** `timeout_s` + a **kill switch**. `breached()` returns the first tripped reason; the orchestrator (T10) turns a breach into `system.halt` + stop.
- **Pin the cost field:** the P0 `agent.step` `usage` comes from the `claude -p` result JSON — sum `usage["total_cost_usd"]` (verified present in `claude_code._parse_usage`); missing/None counts as 0 so a malformed step can't crash the rail.
- **Accumulate incrementally, don't re-scan:** `read_events` defaults to `limit=50`, so a naive "sum all `agent.step` each tick" projection freezes at 50 rows and the rail stops tripping. Keep a `since_id` cursor + a running dollar total, reading only new `agent.step` events per tick (same probe pattern as T11).
- **Kill switch:** a `system.kill` event in the log (owner injects it via the MCP/a CLI) trips `breached()` immediately — the log is already the control channel, so no new mechanism. `timeout_s` is wall-clock from `run.start`. So `breached()` does two cheap reads per tick: new `agent.step` events by cursor (cost), plus a `system.kill` existence probe.
- **Forward note (T15):** T15 will make the wall-clock rail *exclude paused time* — leave the elapsed calc factored so that subtraction is a one-line add, not a retrofit.
- `system.halt` is only observed *between* steps — that's why T13 exists as a second, in-turn rail.
- **Self-check:** feed synthetic `agent.step` events with known `total_cost_usd`; assert the running total trips `usd_budget` at the right point and survives >50 steps; assert a `system.kill` event and an elapsed `timeout_s` each trip with the correct reason; assert a step with no `total_cost_usd` counts as 0.

### T13 — per-session `max_turns` rail in `claude_code.py` (§3.1)
- Wire **`--max-turns`** (and a per-session cost cap if the CLI exposes one) onto the `claude -p` subprocess so a single runaway turn — native subagents fanning out — can't blow the global budget before the kernel reacts between steps.
- Sourced from the role/run config (`max_turns` per role, sane default). Small change to the T5 runtime; the P0 single-agent path keeps working with the default.
- **Self-check:** against the existing fake-subprocess harness used in T5's tests, assert the built `claude -p` argv carries `--max-turns <n>` from the role config and falls back to the default when unset.

### T14 — `topologies/supervisor.yaml` + run-config loader
- Roles as **data** (`name`, `subscribes_to`, `emits`, `prompt`, `runtime`, optional `model`/`max_turns`) + seed task + rails (`usd_budget`, `timeout_s`, `quiescence_s`) — the run config the orchestrator loads. Extend `role.load_role` (already drops unknown keys) rather than a new parser.
- **Supervisor role:** subscribes `task.created` + `claim.made`; on `task.created` decomposes the goal into 2–3 `task.assigned` sub-questions; on enough `claim.made` back, aggregates and emits `run.complete`. **Worker role:** subscribes `task.assigned`; answers each via `claim.made`. Pure-log — no artifacts.
- **The supervisor is stateless — this is the hard part of T14.** Each `ClaudeCodeAgent.step()` is a fresh `claude -p` with no memory of the sub-questions it emitted last step, so "aggregate once enough claims arrive" cannot work from the delivered `claim.made` events alone. The supervisor **prompt** must, each step, do an ad-hoc `read_events` of its *own* prior history (the MCP read returns own events, §5) — the `task.assigned` it issued and the `claim.made` seen so far, keyed by `correlation` — decide whether every sub-question is answered, and only then emit `run.complete`. The log is the memory; the prompt has to actually use it. Nail this in the prompt or the run never terminates cleanly.
- **Acceptance test for the substrate (§7):** going supervisor → critique later must touch **only** YAML + prompts. If Phase 1 forces a kernel/schema change to express this topology, the abstraction is wrong — fix it here.

### T15 — fleet-wide rate-limit rail
- **Verify first (first step of T15):** confirm the real `claude -p` rate-limit result JSON shape live — `claude_code._rate_limit_wait_s` still carries a `ponytail:` note that the schema was never validated against a real limit. If the reset time isn't parseable, the rail can't compute `reset_at`; find that out before wiring the gate.
- Lift P0's in-`step()` single-agent wait-and-resume (`claude_code.py`) to a **kernel rail**: when a runtime hits an API/usage limit it reports `reset_at` up to the kernel instead of sleeping alone; the kernel pauses the affected agents (the **whole fleet** in P1) via a shared `asyncio.Event` resume-gate each loop awaits at the top of its tick, and reopens the gate at `reset_at`.
- **Channel + retry semantics:** `step()` raises `RateLimitError(reset_at)` instead of sleeping — **remove the internal sleep in `_invoke_with_retry`**, don't leave it running alongside the gate. The poll loop catches it and, crucially, **does not advance the cursor and does not emit an `agent.step` for the failed attempt**; after the gate reopens it re-runs `step()` with the *same* event window. (The cursor is a local in `run_agent`, not in the log — the coroutine stays alive across the pause, so "state is in the log" is only true for *restart* recovery, not this in-place resume. State it explicitly so an implementer doesn't drop the window.)
- **Wall-clock during a pause (interaction with T12):** decide and state whether `timeout_s` keeps ticking while the gate is closed — a 1-hour reset will trip a shorter wall-clock. Default: **exclude paused time** from the wall-clock rail (the fleet did no work), so a throttle doesn't masquerade as a runaway.
- `# ponytail:` mark the ceiling — **whole-fleet pause**, upgrade to per-limit-key (per-account) pausing if agents ever span multiple keys.
- **Self-check:** a fake runtime raising `RateLimitError(reset_at)` closes the gate for all loops; assert no `agent.step` is recorded for the failed attempt, the cursor is unchanged, the gate reopens at `reset_at`, and the same event window is re-delivered on resume.

### T16 — two-worker claim-protocol demo (risk center)
- A topology (`topologies/supervisor_claim.yaml`) with a supervisor + **two workers of the same role, distinct identities** (`worker-1`, `worker-2`) subscribed to `task.assigned`. Distinct names are required: two agents sharing one `name` would emit `agent.step` under one identity with colliding `step_n`, corrupting per-agent projections and the replay record (§4). They share the prompt and subscription; only `name` differs. `role.load_role` today models neither duplicate names nor an instance count — pick one (two role entries with a shared prompt, or an `instances: N` field on the role) and note it as part of this task.
- Worker on `task.assigned`: emit `task.claimed` **referencing the task via `correlation`** (= the `task.assigned` event id — pin this key so the ad-hoc read filters correctly), then **one ad-hoc `read_events`** (the MCP one — returns own events) filtered to `task.claimed` for that `correlation`; **lowest event id wins**, the loser backs off and emits nothing. No duplicate `claim.made` for one task (§4).
- The commit rule falls out of the serialized-append invariant: the moment `emit_event` returns id `X`, every id < `X` is already visible — one read, zero wait, no cursor dependency.
- **Verify:** run the topology, assert exactly one `claim.made` per `task.assigned` across both workers. This is model-driven claim logic (prompt + MCP read), so the risk is model reliability — the assertion is the guard.

---

## Exit criterion

`docker compose up` → orchestrator loads `topologies/supervisor.yaml` → the supervisor decomposes the goal into sub-questions, the worker answers each, the supervisor aggregates and emits `run.complete`; the two agents solve the task with **zero direct calls between them** (only through the log); the rails enforce `usd_budget` + wall-clock (breach → `system.halt`); the separate two-worker run shows **lowest-id-wins prevents duplicate work**; every step shows in **Langfuse** and the **`events` table**, and the table holds complete `agent.step` records — the episode is *recorded* replayably (playback engine is Phase 3).

---

## Sequencing

```
T10 ─┬─ T11 ─┐
     └─ T12 ─┴─ T13 ─→ T14 ──→ T15 ──→ T16
                       (first real
                        2-agent run)
```

`T10` (orchestrator skeleton) first; `T11` (termination) + `T12` (budget) are the rails it drives and can land in parallel; `T13` (in-turn `max_turns`) rounds out the rails; `T14` is the first real two-agent run; `T15` (fleet rate-limit) and `T16` (claim demo) build on a working fleet.

Two risk centers: **T11** (quiescence that excludes mid-step agents — get it wrong and demos hang or truncate) and **T16** (claim-protocol reliability with real models). The rest is plumbing.
