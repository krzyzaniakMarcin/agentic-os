# Phase 0 — One Agent, End to End

**Status:** Plan v0.1
**Date:** 2026-07-14
**Parent:** [agentic-os-architecture.md](./agentic-os-architecture.md) §9
**Goal:** `docker compose up` → one Claude Code agent completes a real task through the substrate; every step is visible in Langfuse and the `events` table, and the episode is *recorded* replayably.

---

## Locked decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Replay scope | **Record-only** | Log contains everything to replay later (`agent.step` with `saw_events` + `usage`); playback engine + id-remapping stay in Phase 3. Closes the "unreplayable forever" risk without pulling P3 forward. |
| Entry point | **Thin `scripts/run_phase0.py`** | `orchestrator.py` is P1. Throwaway runner seeds the task and drives one agent; absorbed by the orchestrator later. |
| Demo task | **Pure-log task** (`claim.made` → `run.complete`) | No artifact/git dependency; exercises only the Phase 0 surface (emit/read/agent.step). |
| CC runtime | **Stateless per-step `claude -p` subprocess** | The log IS the memory. Simplest fit for "harness owns the loop". |

---

## Scope boundary

**In:** event log + MCP (emit/read), harness poll loop, one stateless Claude Code runtime, `agent.step` recording, Langfuse tracing, docker compose (db + kernel), a throwaway runner.

**Deferred:** orchestrator/rails (P1), agent memory / KB / artifacts (P2), playback engine + original→replay id-map + `test_replay.py` (P3). Phase 0 only *records* enough to replay later.

---

## Task list

### T1 — `docker-compose.yml` + `sql/init/` + `.env.example` ✅ done
- `db` (`pgvector/pgvector:pg16`), schema auto-applied from `./sql/init` on first boot.
- `kernel` service; Dockerfile installs Python deps + Claude Code CLI.
- Postgres healthcheck + a wait-for-db retry in startup (`depends_on` ≠ "ready").
- **Only the `events` table this phase** — skip memory/kb DDL until those phases use them.

### T2 — `substrate/log.py` (the core invariant — risk center) ✅ done
- Append-only `events` insert/read; every emit takes a **transaction-level advisory lock** (`pg_advisory_xact_lock`) around the insert → monotonic visibility holds across *any* number of writer processes/connections (§4). This matters because writers are plural even in P0: each stdio MCP server instance (T3) plus the harness's own `agent.step` emits.
- `read_events(since_id, types, correlation, limit, exclude_agent, run_id)` with glob→SQL type matching. `exclude_agent` + `run_id` are library params the §6 loop passes (T4); the MCP tool (T3) omits `exclude_agent` and derives `run_id` from the connection.
- `{"v": 1, ...}` payload envelope stamped on emit.
- **Runnable self-check** (assert-based): two connections emitting concurrently — a reader that has seen id N never later observes a new id < N (the advisory lock makes this exercisable for real), plus glob-filter correctness.

### T3 — `substrate/mcp_server.py` (the syscall boundary)
- Exposes **`emit_event` + `read_events` only** (memory/kb/artifacts are later phases).
- **Server-side identity stamping** — per-session, never trust an agent-supplied `agent`.
- **`run_id` derived from the connection**, not a client param.
- **Connection identity for stateless subprocesses:** each `claude -p` step spawns a fresh stdio MCP connection, so the server learns *which agent / which run* from **env vars (`AGENT_NAME`, `RUN_ID`) set on the subprocess** and inherited by the stdio server it spawns. Trivial, but decide it before T3/T5 integration or it stalls there.
- Stdio-per-session means **multiple server processes = multiple writer connections**. The monotonic-visibility invariant is carried by the advisory lock in `log.py` (T2), *not* by connection count — no shared long-running server needed.
- Self-exclusion does *not* live here — it's the harness's job (§6).

### T4 — `agent/poll_loop.py` + `agent/role.py` + `agent/base.py`
- Shared `run_agent` loop (§6): cursor per agent, `read_events(exclude_agent=self unless see_own_events)`, invoke `step()` only on new events.
- **Who emits what (resolve before T3/T5):** for the Claude Code runtime the *model* emits `claim.made` / `run.complete` directly via the substrate MCP (identity stamped server-side). So `step()` returns **`usage` only** (`emits=[]`), and the harness loop emits **only `agent.step`** — no double-emit. The `step()->(emits, usage)` contract stays (other runtimes like `FunctionAgent` will return real emits), but the CC loop's per-emit branch is inert in Phase 0.
- **`agent.step` with `saw_events` + `usage` every step** (§4). Replay bookkeeping still works: the model's emits land *between* consecutive `agent.step` records, so each step's output window is well-defined even though the harness didn't emit them.
- `base.py` = the `step(new_events) -> (emits, usage)` contract; `role.py` = role dataclass + loader.

### T5 — `agent/runtimes/claude_code.py`
- Stateless `step()`: build prompt from `new_events`, run `claude -p` subprocess.
- Uses the **checked-in `config/claude/` MCP config** (not personal `~/.claude`); auth via `ANTHROPIC_API_KEY`.
- Parse `usage` from result JSON (feeds the harness's `agent.step`).
- The model emits `claim.made` + `run.complete` through the substrate MCP (see T4 — `step()` returns usage only).
- **Set Claude Code's own OTel env vars on the subprocess** (telemetry endpoint → Langfuse OTLP) so per-model/per-tool spans *inside* the subprocess reach Langfuse. Without this you get one span per step, not per call.

### T6 — `observability/tracing.py` (second risk center — not plumbing)
- OTel → **local self-hosted Langfuse** — point `.env` at the existing local instance (host URL + keys). Don't add the heavy 4-container stack to this compose file; reuse what's already running.
- Model + tool calls happen *inside* the `claude -p` subprocess, so per-call spans require configuring **Claude Code's own OTel export** on that subprocess (T5), pointed at Langfuse's OTLP endpoint — a second integration with its own failure modes.
- **Honest Phase 0 scope:** step-level spans + `usage` from `tracing.py` guaranteed; per-tool-call spans come from the CC OTel export when it's wired. Exit criterion ("every step visible in Langfuse") is met by step-level; per-call is the stretch.
- **Verify in the first hour of T6** that CC's OTel export actually emits *trace spans* (not only metrics/logs). If it's metrics/logs only, per-tool-call spans are unreachable via env vars — kill the stretch goal then, not after wiring endpoints.

### T7 — `scripts/run_phase0.py` (throwaway)
- Emit `run.start` + one seed `task.created`; start one `run_agent`; wait for `run.complete` **under an `asyncio.wait_for` timeout** so a wedged agent doesn't hang the demo forever; dump the `events` table.
- Absorbed by `orchestrator.py` in P1.

### T8 — Langfuse self-host in `docker-compose.yml` ✅ done
- Owner decision: run Langfuse *in this compose*, not Cloud / an already-running instance (supersedes the T6 "reuse what's already running" note and arch §12's "start with Cloud").
- Implemented as a **separate `langfuse/docker-compose.yml`** (`langfuse-web`, `langfuse-worker`, `langfuse-clickhouse`, `langfuse-redis`, `langfuse-minio`, `langfuse-db`) that the top-level compose pulls in via `include:`. Services are `langfuse-`prefixed so nothing collides with the app's `db`; only `langfuse-web:3000` is published.
- **Self-contained URLs/creds:** every internal URL and credential in the Langfuse file is a literal, *not* `${VAR}` — otherwise the app's `.env` (`POSTGRES_*`, `DATABASE_URL`) would leak in and point Langfuse's migrations at the app's events DB. Verified: kernel → `@db/agentic_os`, Langfuse → `@langfuse-db/langfuse`.
- `LANGFUSE_INIT_*` auto-provisions org `agentic-os` / project `agentic-os` on first boot with known keys; `.env.example` `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` match them, so T6 tracing works with zero manual UI setup. Verified: kernel authenticates to `langfuse-web:3000` and reads the project.
- Included unconditionally (not behind a profile) per owner request, so `docker compose up` now brings the full observability stack up too — heavier first boot (pulls ClickHouse/MinIO/Redis) is the accepted trade.
- Sequence: independent of T2–T5; lands before T6. `T1 → T8 → … → T6`.

---

## Exit criterion

`docker compose up` → `run_phase0.py` → the agent reads the seed task, emits `claim.made` (the answer) + `run.complete`; every step shows in **Langfuse** and the **`events` table**, and the table holds complete `agent.step` records (`saw_events` + `usage`) — the episode is *recorded* replayably, even though the playback engine is Phase 3.

---

## Sequencing

```
T1 → T2 → T3        (substrate first, bottom-up)
        → T4 → T5   (agent; T6 alongside T5)
                → T7 (wire the demo last)
```

Two risk centers: **T2** (the monotonic-visibility invariant) and **T6** (Claude Code OTel export reaching Langfuse from inside the subprocess). The rest is plumbing.
