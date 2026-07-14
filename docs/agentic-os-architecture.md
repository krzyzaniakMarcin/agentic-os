# Agentic OS — Architecture Sketch

**Status:** Draft v0.1
**Date:** 2026-07-14
**Scope:** Personal / local system that automates the owner's workflows **and** doubles as a research substrate for experimenting with multi-agent coordination.
**Non-goals:** Multi-tenancy, hard security isolation, exactly-once durability, high availability. Single trusted user — spend the complexity budget on coordination, not on the two hardest OS problems.

---

## 1. Guiding principles

1. **Agents are Claude Code sessions.** Each agent is one headless Claude Code process (Agent SDK or `claude -p`). Intra-agent decomposition uses Claude Code's native subagents/skills/MCP. Inter-agent coordination is *ours* to build.
2. **All inter-agent communication goes through an append-only event log.** No direct agent-to-agent RPC, ever — even when it feels like overkill. The log is the substrate; that constraint is what makes topologies swappable and episodes replayable.
3. **The kernel is deliberately dumb.** Intelligence lives in agents and in the coordination protocol, never in the kernel. A smart kernel becomes an uncontrolled variable across experiments.
4. **Topology is data, not code.** Supervisor / peer-critique / debate / blackboard / market are all expressed as *role prompts + subscription rules*, not kernel branches.
5. **Observable from commit one.** If a coordination episode can't be traced and replayed, it doesn't count as research.
6. **Flexibility > durability, with one-command reproducibility.** The whole system comes up with `docker compose up`. **Postgres + `pgvector` is the single datastore** — event log, agent memory, and knowledge base all live in one place. Filesystem + git for artifacts. No Temporal, no real queue, no Kubernetes. Optimize the iteration loop.

---

## 2. System overview

```
                    ┌───────────────────────────────────────────────┐
                    │                   KERNEL                        │
                    │  (thin asyncio orchestrator, ~200 LOC)          │
                    │  - spawns/kills agent sessions                  │
                    │  - dispatches events to subscribers             │
                    │  - enforces rails: max turns, $ ceiling, kill   │
                    └───────────────┬───────────────────────────────┘
                                    │ spawns
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
 ┌──────────────┐           ┌──────────────┐           ┌──────────────┐
 │  Agent A     │           │  Agent B     │           │  Agent C     │
 │ (Claude Code │           │ (Claude Code │           │ (Claude Code │
 │  session)    │           │  session)    │           │  session)    │
 │  + role      │           │  + role      │           │  + role      │
 │  + native    │           │  + native    │           │  + native    │
 │   subagents  │           │   subagents  │           │   subagents  │
 └──────┬───────┘           └──────┬───────┘           └──────┬───────┘
        │ emit_event / read_events (via MCP)                   │
        └───────────────────────────┬──────────────────────────┘
                                     ▼
                      ┌──────────────────────────────┐
                      │   SHARED SUBSTRATE            │
                      │  - Event log    (Postgres)    │  ← the "blackboard" + message bus
                      │  - Agent memory (Postgres     │
                      │      + pgvector)              │
                      │  - Knowledge base (pgvector)  │  ← you populate; agents query
                      │  - Artifact memory (git repo) │  ← agents commit outputs here
                      └──────────────────────────────┘
                                     │ OTel spans
                                     ▼
                            ┌──────────────────┐
                            │    Langfuse      │  ← per-call traces + coordination metrics
                            └──────────────────┘
```

The **event log is the bus and the blackboard at once**: agents publish by appending, subscribe by reading a filtered view. There is no separate message broker.

---

## 3. Core components

### 3.1 Kernel
A single asyncio process. Responsibilities, and nothing more:

- **Spawn** N agent sessions from a run config, each with a role prompt and a subscription filter.
- **Dispatch**: tail the event log; when a new event matches an agent's subscription, wake that agent with the new events (or agents poll — see §6).
- **Rails**: enforce per-agent `max_turns`, a global `usd_budget`, wall-clock timeout, and a kill switch. On breach, emit a `system.halt` event and terminate sessions.
- **Lifecycle**: record run start/end, seed the initial task event, detect termination (a `run.complete` event or quiescence — no new events for T seconds).

The kernel does **not** decide who works next, does not summarize, does not route by content semantics. Routing is purely mechanical (subscription filters). Anything smarter belongs in an agent.

### 3.2 Agent
An agent = one Claude Code session + a **role definition**. The role defines:

- `system_prompt` / role instructions (what this agent is for)
- `subscribes_to`: list of event-type globs (e.g. `task.*`, `claim.*`)
- `emits`: declared event types (documentation + validation)
- `tools`: which MCP servers/skills it can use
- optional `model`, `max_turns`, `temperature`

The **only** way an agent talks to another agent is `emit_event`; the only way it hears anyone is `read_events`. Both are exposed as an MCP server (§5). Native Claude Code subagents are fine *inside* the session for decomposition, but they never cross the agent boundary.

### 3.3 Shared substrate
Backed by a **single Postgres instance with the `pgvector` extension** — event log, agent memory, and knowledge base all live here, so there's one thing to run, back up, and query.

- **Event log** — append-only Postgres table. The source of truth for coordination. (§4)
- **Agent memory** — key/value + vector store for facts agents write/recall *during* runs. Agent-authored, lower trust, read/written *through the interface*, never touched directly.
- **Knowledge base** — a curated, *you*-authored corpus that agents can query but not silently overwrite. Authoritative, higher trust, its own lifecycle. (§3.6)
- **Artifact memory** — a git repo where agents commit files (code, docs, data). Diffable, inspectable, replayable. The git history is a second, human-friendly trace.

### 3.4 Observability
Langfuse via OpenTelemetry. Two layers of instrumentation:

- **Agent layer** (standard): every model call + tool call as spans, with tokens and cost.
- **Coordination layer** (the research payload): rounds-to-converge, cost per episode, message counts, who-talked-to-whom, topology label. These come almost for free by projecting the event log.

### 3.5 Agent runtimes (heterogeneous fleet)
An *agent* is defined by its **contract** — an identity, a subscription, and read/emit through the substrate — **not** by what runs inside it. Claude Code is one runtime among several. All implement the same `Agent` adapter (`step(new_events) -> emits`):

- **ClaudeCodeAgent** — heavy, coding/tool-using, native subagents. Build/refactor/multi-step work.
- **LLMAgent** — a single model loop (Agent SDK or raw API) with whatever MCPs it needs. Cheap, fast, no filesystem overhead. *E.g. the Gmail inbox summarizer.*
- **FunctionAgent** — pure Python, no model. Triggers, routers, formatters, schedulers. Emits task events on a schedule or webhook.
- **ForeignAgent** — wraps something else (a LangGraph graph, an external process) behind the same contract.

Because agents only touch the world through the event log, adding a runtime is **one adapter and zero changes** to kernel or substrate. LLM-based agents reach the log via the `substrate` MCP; `FunctionAgent`s call the log library directly.

Example — a Gmail summarizer plus the cron trigger that wakes it:
```yaml
- name: morning_trigger
  runtime: function          # no model; emits on a schedule
  schedule: "0 7 * * *"
  emits: ["task.summarize_inbox"]
- name: inbox_summarizer
  runtime: llm
  model: claude-haiku        # cheap; just read + summarize
  tools: [gmail, substrate]  # domain MCP + the coordination MCP
  subscribes_to: ["task.summarize_inbox"]
  emits: ["claim.made", "artifact.written"]
```

### 3.6 Knowledge base (curated, you-authored)
A retrieval corpus that *you* populate and agents *query*. Distinct from agent memory: authoritative, curated, and not silently overwritten by agents.

- **Ingestion** — you drop files (notes, docs, PDFs, snippets) into `knowledge/`, or push via an ingest CLI/MCP tool. A pipeline chunks → embeds → stores in `pgvector` with source metadata.
- **Query** — agents call a `kb_query` tool (semantic + optional keyword/metadata filter) and get back passages *with citations* (source path + chunk id) so answers stay traceable to what you actually wrote.
- **Trust boundary** — agents may *read* the KB and *propose* additions (as `kb.suggestion` events for your review); only an explicit ingest step commits new authoritative knowledge.

Tables (same Postgres, `pgvector`):
```sql
CREATE TABLE kb_documents (
    id         BIGSERIAL PRIMARY KEY,
    source     TEXT NOT NULL,           -- file path / URL / origin
    title      TEXT,
    meta       JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE kb_chunks (
    id          BIGSERIAL PRIMARY KEY,
    document_id BIGINT REFERENCES kb_documents(id) ON DELETE CASCADE,
    chunk_index INT  NOT NULL,
    content     TEXT NOT NULL,
    embedding   VECTOR(1536)            -- match your embedding model's dims
);
CREATE INDEX idx_kb_chunks_embed ON kb_chunks
    USING ivfflat (embedding vector_cosine_ops);
```

---

## 4. Event log schema

One table is enough to start. Append-only; never update or delete rows.

```sql
CREATE TABLE events (
    id          BIGSERIAL   PRIMARY KEY,             -- global monotonic order
    run_id      TEXT        NOT NULL,                -- which episode
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),  -- emit time
    agent       TEXT        NOT NULL,                -- emitter ("kernel" for system events)
    type        TEXT        NOT NULL,                -- dotted namespace, e.g. "claim.made"
    payload     JSONB       NOT NULL,                -- structured payload (queryable)
    reply_to    BIGINT,                              -- optional: id of event this responds to
    correlation TEXT                                 -- optional: thread/task id to group related events
);

CREATE INDEX idx_events_run     ON events(run_id, id);
CREATE INDEX idx_events_type    ON events(run_id, type);
CREATE INDEX idx_events_corr    ON events(run_id, correlation);
CREATE INDEX idx_events_payload ON events USING gin (payload);  -- query into JSON
```

### Event type conventions (namespaced, extensible)
| Type | Emitter | Meaning |
|------|---------|---------|
| `run.start` | kernel | Episode begins; payload has run config |
| `task.created` | kernel / agent | A unit of work is available |
| `task.assigned` | supervisor agent | Task handed to a worker |
| `claim.made` | worker agent | An assertion / proposed answer |
| `critique.made` | reviewer agent | Feedback on a claim |
| `vote.cast` | any agent | Support/oppose in a decision protocol |
| `artifact.written` | any agent | A file was committed to artifact memory (payload: path, git sha) |
| `memory.write` / `memory.read` | any agent | Structured memory access |
| `run.complete` | any agent / kernel | Terminal state reached |
| `system.halt` | kernel | Rail breached; agents must stop |

**Design rule:** a new coordination protocol should be expressible by adding new `type` values and new subscription filters — *not* by changing the schema or the kernel.

### Replay
Because the log is append-only and globally ordered, an episode replays deterministically up to model nondeterminism. For strict replay, also log each agent's model responses (`llm.response` events) so a replay can be run in "playback" mode that returns recorded outputs instead of calling the model. This is your regression harness.

---

## 5. Agent-facing MCP interface (the "syscall boundary")

Exposed to every agent as an MCP server named `substrate`. This is the stable interface all agents call through; capability checks live here.

```jsonc
// Tool: emit_event
{
  "name": "emit_event",
  "description": "Publish an event to the shared log. The only way to communicate with other agents.",
  "input": {
    "type":        "string",   // e.g. "claim.made"
    "payload":     "object",   // arbitrary JSON
    "reply_to":    "integer?", // optional event id being responded to
    "correlation": "string?"   // optional thread/task id
  },
  "returns": { "id": "integer", "ts": "number" }
}

// Tool: read_events
{
  "name": "read_events",
  "description": "Read events from the shared log, filtered. Use to observe other agents.",
  "input": {
    "since_id":    "integer?",   // return events with id > since_id (cursor)
    "types":       "string[]?",  // glob filters, e.g. ["claim.*","critique.*"]
    "correlation": "string?",    // restrict to one thread
    "limit":       "integer?"    // default 50
  },
  "returns": "event[]"
}

// Tool: memory_write / memory_read  (AGENT working memory — agent-authored, lower trust)
{ "name": "memory_write", "input": { "key": "string", "value": "object", "tags": "string[]?" } }
{ "name": "memory_read",  "input": { "query": "string", "k": "integer?" }, "returns": "record[]" }

// Tool: kb_query  (CURATED knowledge base — you-authored, read-only to agents)
{ "name": "kb_query",
  "input":   { "query": "string", "k": "integer?", "filter": "object?" },
  "returns": "passage[]" }  // each passage: { content, source, document_id, chunk_index, score }

// Tool: write_artifact  (commit a file to the git artifact repo)
{ "name": "write_artifact", "input": { "path": "string", "content": "string", "message": "string" },
  "returns": { "sha": "string" } }  // also auto-emits an artifact.written event
```

Notes:
- `read_events` takes a **cursor** (`since_id`), so agents poll incrementally without re-reading history.
- **`memory_*` vs `kb_query`**: memory is agent scratch state (read+write); the knowledge base is your curated corpus (read-only to agents, returns cited passages). Agents propose KB additions via `kb.suggestion` events, not by writing directly.
- `write_artifact` auto-emits `artifact.written`, keeping the log the single source of truth for "what happened".
- Capability enforcement (which agent may emit which types, touch which memory namespaces) is a thin allow-list checked here — cheap now, and the seam is in place if you ever want real isolation later.

---

## 6. Kernel dispatch model

Two viable models; start with polling for simplicity, graduate to push if latency bites.

**A. Poll (start here).** Agents loop: `read_events(since_id=cursor)`, act, `emit_event`, repeat until they see `run.complete`/`system.halt` or their subscription yields nothing for a while. The kernel just supervises rails and detects quiescence. Dead simple, fully inspectable, no callbacks.

**B. Push (later).** Kernel tails the log, matches each new event against subscriptions, and wakes only the relevant agent(s). Lower latency and token cost, but adds scheduling logic. Only add this once you feel poll-loop waste.

Pseudocode for the dumb kernel (poll model):

```python
async def run_episode(cfg):
    log.emit("kernel", "run.start", cfg.dict())
    log.emit("kernel", "task.created", cfg.seed_task)

    agents = [spawn_agent(role, cfg.run_id) for role in cfg.roles]
    budget = Budget(usd=cfg.usd_budget, wall_s=cfg.timeout_s)

    async with agents:
        while not terminated(cfg.run_id):
            if budget.breached():
                log.emit("kernel", "system.halt", {"reason": budget.reason})
                break
            await asyncio.sleep(cfg.tick_s)   # agents self-drive via poll loop
    return summarize(cfg.run_id)   # projection over the event log
```

The kernel never inspects event *content* to make routing decisions. That is the invariant that keeps experiments clean.

---

## 7. Topologies as configuration

Same kernel, same agents, same substrate — only role prompts + subscriptions change. Examples:

### 7.1 Supervisor / worker (task automation default)
```yaml
run_id: demo-supervisor
seed_task: { goal: "Refactor module X and add tests" }
roles:
  - name: supervisor
    subscribes_to: ["task.created", "claim.made"]
    emits:         ["task.assigned", "run.complete"]
  - name: worker
    subscribes_to: ["task.assigned"]
    emits:         ["claim.made", "artifact.written"]
```

### 7.2 Peer critique (two agents improve each other)
```yaml
run_id: demo-critique
seed_task: { goal: "Answer question Q with best reasoning" }
roles:
  - name: solver
    subscribes_to: ["task.created", "critique.made"]
    emits:         ["claim.made"]
  - name: critic
    subscribes_to: ["claim.made"]
    emits:         ["critique.made", "run.complete"]
```

### 7.3 Debate (N agents, moderator tallies)
```yaml
run_id: demo-debate
roles:
  - name: debater_pro   { subscribes_to: ["task.created","claim.made"], emits: ["claim.made"] }
  - name: debater_con   { subscribes_to: ["task.created","claim.made"], emits: ["claim.made"] }
  - name: moderator     { subscribes_to: ["claim.made"], emits: ["vote.cast","run.complete"] }
```

**Acceptance test for the substrate:** going from 7.1 to 7.2 should require touching only YAML + role prompts. If it forces kernel or schema changes, the abstraction is wrong — fix it before building more.

---

## 8. Repository structure

```
agentic-os/
├── README.md
├── docker-compose.yml       # `docker compose up` → db + kernel (+ optional langfuse)
├── Dockerfile               # kernel image: Python + Claude Code CLI
├── .env.example             # ANTHROPIC_API_KEY, DATABASE_URL, embed model + dims
├── pyproject.toml
├── sql/
│   └── init/                # schema: events, memory, kb_* (auto-run on db first boot)
├── kernel/
│   ├── __init__.py
│   ├── orchestrator.py      # spawn/dispatch/rails (the dumb kernel)
│   ├── budget.py            # $ + wall-clock + turn ceilings
│   └── termination.py       # quiescence / run.complete detection
├── agent/
│   ├── base.py              # Agent adapter contract: step(new_events) -> emits
│   ├── runtimes/
│   │   ├── claude_code.py   # ClaudeCodeAgent (headless session)
│   │   ├── llm.py           # LLMAgent (single model loop + MCPs) — e.g. Gmail summarizer
│   │   ├── function.py      # FunctionAgent (no model: triggers, routers, schedulers)
│   │   └── foreign.py       # ForeignAgent (wrap LangGraph / external process)
│   ├── role.py              # role definition dataclass + loader
│   └── poll_loop.py         # default read→act→emit loop
├── substrate/
│   ├── log.py               # append-only event log (Postgres)
│   ├── memory.py            # agent working memory (Postgres + pgvector)
│   ├── kb.py                # knowledge base: ingest + kb_query (pgvector)
│   ├── artifacts.py         # git-backed artifact store
│   └── mcp_server.py        # exposes emit_event/read_events/memory/kb_query/write_artifact
├── ingestion/
│   └── ingest.py            # chunk → embed → upsert into kb_* ; watches knowledge/
├── knowledge/               # you drop source docs here (notes, PDFs, snippets)
├── observability/
│   └── tracing.py           # OTel → Langfuse; coordination-metric projections
├── topologies/
│   ├── supervisor.yaml
│   ├── critique.yaml
│   └── debate.yaml
├── experiments/
│   ├── datasets/            # fixed task sets for replay
│   └── run.py               # run a topology against a dataset, score, log to Langfuse
├── data/
│   └── artifacts/           # git repo agents commit into (Postgres data lives in a volume)
└── tests/
    └── test_replay.py       # determinism / playback-mode regression harness
```

---

## 9. Build roadmap

### Phase 0 — one agent, end to end (days 1–3)
- `docker-compose.yml`: `db` (pgvector) + `kernel` come up with one command.
- `agent/runtimes/claude_code.py`: spawn a headless Claude Code session, give it a role + task, capture output.
- `substrate/log.py` + `substrate/mcp_server.py`: Postgres event log with `emit_event` / `read_events` over MCP.
- `observability/tracing.py`: Langfuse tracing on every model + tool call.
- **Exit criterion:** `docker compose up`, one agent completes a real task, and every step is visible in Langfuse and in the `events` table.

### Phase 1 — two agents, one topology (days 4–7)
- `kernel/orchestrator.py` (poll model) + `agent/poll_loop.py`.
- `topologies/supervisor.yaml`: supervisor + worker on one task, coordinating only through the log.
- **Exit criterion:** the two agents solve a task with zero direct calls between them; the log fully explains the episode.

### Phase 2 — prove the substrate: heterogeneity, KB, second topology (week 2)
- Add a second runtime: `agent/runtimes/llm.py` + a `FunctionAgent` trigger — ship the **Gmail inbox summarizer** as the first non-Claude-Code agent. Proves the runtime abstraction.
- Add `substrate/kb.py` + `ingestion/ingest.py` + `kb_query`; ingest your first docs into `pgvector` and have an agent answer from them (with citations).
- Add `topologies/critique.yaml` reusing the same agents/kernel/log — change **only** YAML + prompts.
- Add `substrate/artifacts.py` + `write_artifact`.
- **Exit criterion:** swapping supervisor→critique needs no kernel/schema change, and a Claude Code agent + a Haiku summarizer coordinate through the same log. This validates the whole design.

### Phase 3 — research harness (week 3+)
- `experiments/run.py`: run a topology against a fixed dataset, score outcomes, push coordination metrics to Langfuse datasets.
- `tests/test_replay.py`: playback-mode determinism.
- Now iterate on protocols (debate, market-based bidding, blackboard) as pure config/prompt experiments.

---

## 10. Failure modes to guard against

- **Direct agent RPC "because it's faster."** It is faster, and it destroys replayability and turns every new topology into a code change. Force all inter-agent traffic through the log — this is the point of the whole system.
- **A kernel that gets smart.** The moment the kernel routes by content or summarizes, it becomes an uncontrolled experimental variable. Keep routing mechanical.
- **Building the OS before an agent does anything useful.** Ship Phase 0 before designing the multi-agent kernel. Let abstractions earn their place by pain, not by anticipation.
- **Skipping observability "until later."** An agentic system you can't trace is one you can't debug or improve. Langfuse in Phase 0, not Phase 3.
- **Reaching for durability.** Temporal/queues/HA are the wrong weight for a single-user research rig and will eat the time that should go to coordination experiments.

---

## 11. Stack summary

| Layer | Choice | Why |
|-------|--------|-----|
| Agent runtimes | Claude Code (headless) **+ LLMAgent / FunctionAgent / ForeignAgent** | Heterogeneous fleet behind one adapter contract; runtime is not part of the "agent" definition |
| Intra-agent orchestration | Claude Code native subagents (+ LangGraph optional, *inside* a step) | Decomposition without owning a workflow engine |
| Inter-agent coordination | Custom append-only event log (Postgres) | Swappable topologies, replay, observability for free |
| Kernel | Thin asyncio orchestrator | Keep intelligence out of the substrate |
| Event log + agent memory + KB | **Postgres + pgvector (one instance)** | One datastore to run/query; JSONB + vector search in one place |
| Knowledge base | Curated corpus in pgvector, queried via `kb_query` | You-authored, cited, agents query but don't overwrite |
| Artifact memory | Git repo | Diffable, human-readable second trace |
| Observability / eval | Langfuse via OTel | Per-call traces + coordination metrics + dataset replays |
| Packaging | **Docker Compose** | `docker compose up` spins up the whole system |

> LangGraph note: keep it as an *optional intra-step* tool, not the backbone. It's a graph executor with checkpointing, not a scheduler or durable job queue — don't let it become the inter-agent runtime.

---

## 12. Running it (Docker Compose)

Goal: `docker compose up` brings up the whole system. Minimal service set:

```yaml
services:
  db:                                   # event log + agent memory + knowledge base
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: agentic_os
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./sql/init:/docker-entrypoint-initdb.d   # schema auto-applied on first boot
    ports: ["5432:5432"]

  kernel:                               # the dumb orchestrator; spawns agents
    build: .                            # Dockerfile installs Python deps + Claude Code CLI
    depends_on: [db]
    env_file: .env                      # ANTHROPIC_API_KEY, DATABASE_URL, embed dims
    volumes:
      - ./:/app
      - ~/.claude:/root/.claude         # Claude Code auth/config into the container
      - ./data/artifacts:/app/data/artifacts
      - ./knowledge:/app/knowledge      # KB source docs
    command: python -m kernel.orchestrator

  # Optional — Langfuse self-host is multi-container (Postgres + ClickHouse + Redis + MinIO).
  # To keep `up` light, point at Langfuse Cloud via env and omit this block to start.
  # langfuse: ...

volumes:
  pgdata: {}
```

Gotchas to plan for:

- **Langfuse self-host is heavy.** Recent versions need ClickHouse, Redis, and MinIO alongside their own Postgres. For a personal rig, start with Langfuse Cloud (just set the keys in `.env`) and add the self-host stack later if you want everything local.
- **Claude Code CLI + auth live inside the `kernel` container.** Mount `~/.claude` (or pass `ANTHROPIC_API_KEY`) so headless sessions can run. Bake the CLI into the `Dockerfile`.
- **Domain MCPs (Gmail, etc.)** run either as sidecar services in the same compose file or via the CLI's MCP config; agents reference them by name in their role's `tools`.
- **Secrets via `.env`, never committed.** Ship `.env.example` with the keys blank.
- **One embedding model choice fixes `VECTOR(dims)`** in the KB schema — pick it before first ingest, or you'll be re-embedding.
- **`depends_on` isn't "wait until ready".** Add a small wait-for-Postgres retry in the kernel's startup, or a healthcheck on `db`.
