# Agentic OS — Architecture Sketch

**Status:** Draft v0.2 (incorporates design review)
**Date:** 2026-07-14
**Scope:** Personal / local system that automates the owner's workflows **and** doubles as a research substrate for experimenting with multi-agent coordination.
**Non-goals:** Multi-tenancy, hard security isolation, exactly-once durability, high availability. Single trusted user — spend the complexity budget on coordination, not on the two hardest OS problems.

---

## 1. Guiding principles

1. **Agents are defined by a contract, not a runtime.** An agent has an identity, a subscription, and talks only through the substrate; Claude Code is the default runtime but not the only one (§3.5). Intra-agent decomposition can use Claude Code's native subagents/skills/MCP. Inter-agent coordination is *ours* to build.
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

- **Spawn** the per-agent poll loops (§6) from a run config, each with a role prompt and a subscription filter.
- **Supervise rails**: enforce a global `usd_budget` (summed from `agent.step` usage, §4), wall-clock timeout, and a kill switch. On breach, emit `system.halt` and stop the agents. Because `system.halt` is only observed *between* steps, also set **per-session** `max_turns`/budget as a second rail inside each runtime — a single runaway Claude Code turn (native subagents fanning out) can otherwise blow the budget before the kernel reacts.
- **Detect termination**: a `run.complete` event, or **quiescence** — no new events for T seconds *and no agent currently mid-step*. Excluding in-flight agents matters: a Claude Code agent can be minutes into a tool-using turn without emitting; naive "no events for T seconds" would fire mid-thought and truncate the run. The kernel owns the sessions, so it knows who is stepping.

The kernel does **not** decide who works next, does not summarize, does not route by content semantics. Routing is purely mechanical (subscription filters). Anything smarter belongs in an agent.

### 3.2 Agent
An agent = one Claude Code session + a **role definition**. The role defines:

- `system_prompt` / role instructions (what this agent is for)
- `subscribes_to`: list of event-type globs (e.g. `task.*`, `claim.*`)
- `emits`: declared event types (documentation + validation)
- `tools`: which MCP servers/skills it can use
- `see_own_events`: default `false` — whether the harness delivers the agent's *own* emissions back to it (needed only by self-observing protocols; §6)
- optional `model`, `max_turns`, `temperature`

The **only** way an agent affects another agent is by emitting events; it perceives others only through events its harness reads for it (§6 — the harness drives the loop, the model doesn't self-poll). Both go through the `substrate` MCP server (§5), which stamps the emitter identity server-side. Native Claude Code subagents are fine *inside* the session for decomposition, but they never cross the agent boundary.

### 3.3 Shared substrate
Backed by a **single Postgres instance with the `pgvector` extension** — event log, agent memory, and knowledge base all live here, so there's one thing to run, back up, and query.

- **Event log** — append-only Postgres table. The source of truth for coordination. (§4)
- **Agent memory** — a store agents write/recall *during* runs. Agent-authored, lower trust, accessed only *through the interface*. **Run-scoped by default** (each run gets a fresh namespace) so episodes stay independent trials; opt into `persistent` namespaces explicitly and record the choice in the run config, so memory scope is a *controlled variable* rather than silent cross-run contamination. It's both a KV store (`memory_read(key=…)`) and a vector store (`memory_read(query=…)`) — two read paths. Who may read whose memory (per-agent namespace vs a shared blackboard namespace) is itself a topology variable worth controlling.
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
    embed_model TEXT NOT NULL,           -- which model produced `embedding`
    embedding   VECTOR(1536)             -- match the model's dims
);
-- HNSW, not ivfflat: no training step, handles incremental inserts cleanly.
-- (ivfflat built on an empty table at init clusters badly and needs periodic rebuilds.)
CREATE INDEX idx_kb_chunks_embed ON kb_chunks
    USING hnsw (embedding vector_cosine_ops);
```

Store `embed_model` per chunk so a model swap is **detectable** — querying with a different model against old vectors silently returns garbage similarities otherwise. On a switch, re-embed rather than mixing spaces.

### 3.7 Human-in-the-loop (the owner is an agent too)
For a personal automation OS this is the biggest functional gap to close early: without it there's no path for the system to reach *you* or you to reach *it* mid-run. Where does the Gmail summary actually land? Who reviews a `kb.suggestion`? The answer is to model **the owner as just another agent** — nothing new in the architecture, it only needs naming and scheduling.

Concretely, a `FunctionAgent` (a CLI, or a watched inbox/table) that:
- **Emits on your behalf** — `task.created`, `approval.granted`, `human.answered` — injecting your input into the run like any other event.
- **Subscribes to what needs you** — `question.asked`, `kb.suggestion`, `artifact.written` on certain paths — and delivers via notification/email.

The Gmail summarizer needs a delivery channel anyway, so this slots naturally into Phase 2 alongside it. It's the "review" mechanism the KB trust boundary (§3.6) refers to.

---

## 4. Event log schema

One table is enough to start. **Append-only** — never update or delete rows; corrections are new events.

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

#### Invariant: monotonic visibility
`BIGSERIAL` ids are assigned at insert but rows only become visible at **commit**. With concurrent emitters a reader can see `id=105` while `id=103` is still uncommitted, advance its cursor past 103, and never see it — a *nondeterministically* lost message, which is exactly what poisons the replay/comparison experiments this system exists for. The property downstream code (cursors, replay, leader election) actually needs is **monotonic visibility**: no event ever becomes visible *after* an event with a higher id has already been read. (Numeric gaps in ids are fine and expected — aborted transactions and sequence caching burn ids without producing rows; cursors tolerate gaps, so never assert on *consecutive* ids.) To guarantee monotonic visibility:

> **All appends go through a single writer connection in the `substrate` MCP server.** At this throughput that fully serializes inserts, so ids become visible in order. (Alternatives if you ever outgrow it: a transaction-level advisory lock around insert, or readers bounding by `pg_snapshot_xmin(pg_current_snapshot())`.)

#### Payload envelope
Every payload carries a version field from day one — `{"v": 1, ...}`. Costless now; retrofitting versioning onto a log you've promised never to rewrite is painful. Bump `v` when a payload shape changes; readers switch on it.

#### Content-reference convention
Big content (files, long outputs, diffs) goes to **artifact memory**; events carry a *reference* (`{"artifact": "<path>", "sha": "..."}`), not the bytes. Otherwise the log bloats and `read_events` windows blow up model contexts.

### Event type conventions (namespaced, extensible)
| Type | Emitter | Meaning |
|------|---------|---------|
| `run.start` | kernel | Episode begins; payload has run config |
| `task.created` | kernel / agent | A unit of work is available (may carry `to:` for a target) |
| `task.assigned` | supervisor agent | Task handed to a worker |
| `task.claimed` | worker agent | Worker claims an open task; lowest event id wins (see below) |
| `claim.made` | worker agent | An assertion / proposed answer |
| `critique.made` | reviewer agent | Feedback on a claim |
| `vote.cast` | any agent | Support/oppose in a decision protocol |
| `artifact.written` | any agent | A file was committed to artifact memory (payload: path, git sha) |
| `agent.step` | harness (as the agent) | One step's execution record: `{step_n, saw_events:[from,to], usage}` — replay + cost. Emitted under the agent's own name so per-agent projections stay trivial. |
| `kb.suggestion` | any agent | Proposed KB addition, awaiting owner review |
| `question.asked` | any agent | Agent needs the owner; the human-in-the-loop agent delivers it (§3.7) |
| `approval.granted` / `human.answered` | owner (via HIL agent) | Owner's response back into the run |
| `run.complete` | any agent / kernel | Terminal state reached |
| `system.halt` | kernel | Rail breached; agents must stop |

**Design rule:** a new coordination protocol should be expressible by adding new `type` values and new subscription filters — *not* by changing the schema or the kernel.

#### Task routing & claiming (avoid duplicate work)
A `task.*` event reaches *every* agent subscribed to that type, so with two workers of the same role both would grab every task. Two ways to disambiguate, both pure convention on the total order:

- **Direct routing** — tasks carry a `to:` field; subscription filtering respects it. Use when the assigner knows the target.
- **Claim protocol** — a worker emits `task.claimed` referencing the task; **lowest event id wins**. To learn whether it won, a claimant must also **subscribe to `task.claimed`** and compare ids before starting work — otherwise it emits a claim and begins immediately, defeating the protocol. The total order gives you leader election for free, and it's a reusable primitive for market/bidding topologies later.

### Replay
Monotonic-visibility order (invariant above) gets you *structural* replay, but you also need **what each agent saw when it stepped**: two concurrent agents interleave reads nondeterministically, so on replay agent B's third step might see 6 events where the original saw 4 — making its recorded output contextually wrong. The `agent.step` event records the id window each agent saw (`saw_events:[from_id, to_id]`) plus its `usage`, turning the log into a **complete execution trace**.

**Playback is log-only — there is no extra capture.** An agent's recorded output for step N *is* the set of events it emitted between its `agent.step` boundaries, and those are already in the log. So `step()` in playback mode = look up and re-emit those events instead of calling the model. Because `saw_events` is a raw id range, replay must re-apply the **role's `types` filter** (and the self-exclusion) over that range to reconstruct exactly what was delivered.

- Call-level playback *inside* a Claude Code step (intermediate tool-use turns) is finer granularity than the log records — that detail lives in **Langfuse traces**, not the event log. Fine, but know the trade: the log replays step-to-step, Langfuse holds the within-step detail.
- `agent.step` is also how the kernel knows spend for `usd_budget` (sum the `usage`). **Record it from Phase 0/1, not Phase 3** — episodes not recorded this way are unreplayable forever.

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

// Tool: memory_write / memory_read  (AGENT working memory — run-scoped by default)
{ "name": "memory_write", "input": { "key": "string", "value": "object", "tags": "string[]?",
                                     "namespace": "string?" } }   // namespace defaults to this run
{ "name": "memory_read",  "input": { "key": "string?",            // exact KV read
                                     "query": "string?",          // OR semantic vector read
                                     "k": "integer?", "namespace": "string?" },
  "returns": "record[]" }

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
- **Every call is implicitly scoped to the calling session's `run_id`** — the server derives it from the connection; there is no client-supplied `run_id` param (which would be spoofable). The `run_id` shown in the §6 pseudocode is the harness's own library call, not the MCP surface. Same for `exclude_agent`, which the server sets from the session identity.
- **`memory_*` vs `kb_query`**: memory is agent scratch state (read+write); the knowledge base is your curated corpus (read-only to agents, returns cited passages). Agents propose KB additions via `kb.suggestion` events, not by writing directly.
- `write_artifact` auto-emits `artifact.written`, keeping the log the single source of truth for "what happened".
- **Emitter identity is stamped server-side.** The server sets `agent` from the session's own identity (per-agent connection/token) and never trusts an agent-supplied value. A confused model claiming to be another agent would silently corrupt coordination data.
- **Artifact writes are serialized and run-isolated.** Concurrent `write_artifact` calls into one git repo race, so all git ops go behind a single lock in the server; use **branch-per-`run_id`** so two runs' histories don't collide and replays don't overwrite originals.
- Capability enforcement (which agent may emit which types, touch which memory namespaces) is a thin allow-list checked here — cheap now, and the seam is in place if you ever want real isolation later.

---

## 6. Dispatch model — the harness drives the loop

**The runtime adapter, not the model, owns the poll loop.** This is the single most important execution decision, and it dictates what `poll_loop.py` and the runtimes actually are.

Two things *could* poll the log: the **model** (by calling `read_events` itself) or the **harness** (the adapter reads on the agent's behalf and invokes the model only when there's something to react to). Make **harness-driven** the rule:

- The adapter owns a **cursor per agent** and calls `read_events(since_id=cursor, types=subscription)` on a tick.
- **The read excludes the agent's own emissions by default** (`WHERE agent != self`). Otherwise an agent that both subscribes to and emits the same type (e.g. `debater_pro` + `claim.made` in §7.3) is woken by its own claim and self-loops until a rail trips. *Don't* fix this by bumping the cursor past your own writes — another agent's events may have interleaved, and skipping them recreates the lost-message problem (§4). Filter by emitter; use the per-role `see_own_events: true` opt-in for protocols that genuinely need self-observation.
- When — and only when — matching new events exist, it invokes the model **once**, injecting those events as the message. That is what makes `step(new_events) -> emits` literally true.
- While idle it costs **zero tokens**. Model-driven polling burns tokens on every empty poll, and models forget to poll or poll obsessively — unreliable and expensive for a long-running Claude Code session.
- `read_events` stays exposed as an MCP tool, but only for the model's **ad-hoc history queries** ("what critiques were already made on this task?"), never as the drive mechanism.

So the loop lives in `agent/poll_loop.py` and is shared by every runtime; each runtime only implements `step()`. `FunctionAgent`s skip the model entirely; `LLMAgent`/`ClaudeCodeAgent` turn `new_events` into a prompt.

```python
# agent/poll_loop.py — one loop, every runtime
async def run_agent(agent, cursor=0):
    while not agent.stopped:
        events = await log.read_events(
            since_id=cursor,
            types=agent.subscribes_to,
            exclude_agent=None if agent.see_own_events else agent.name,  # no self-echo
            run_id=agent.run_id)                              # monotonic-visibility read (§4)
        if events:
            saw = (events[0].id, events[-1].id)
            emitted, usage = await agent.step(events)         # model invoked here, if any
            for e in emitted:
                log.emit(agent.name, e.type, e.payload)       # identity set server-side
            log.emit(agent.name, "agent.step",                # replay + cost record (§4)
                     {"v": 1, "step_n": agent.step_n, "saw_events": saw, "usage": usage})
            cursor = events[-1].id
        else:
            await asyncio.sleep(agent.tick_s)
```

**Push variant (later).** The kernel can tail the log and hand matching events straight to an adapter's `step()` instead of each adapter ticking — lower latency, same contract. Add only once tick-polling waste bites.

```python
# kernel/orchestrator.py — spawns loops, supervises rails; never inspects event content
async def run_episode(cfg):
    log.emit("kernel", "run.start",    {"v": 1, **cfg.dict()})
    log.emit("kernel", "task.created", {"v": 1, **cfg.seed_task})
    agents = [make_agent(role, cfg.run_id) for role in cfg.roles]
    [asyncio.create_task(run_agent(a)) for a in agents]
    budget = Budget(usd=cfg.usd_budget, wall_s=cfg.timeout_s)   # summed from agent.step usage

    while not terminated(cfg.run_id):          # run.complete OR quiescence w/ no agent mid-step (§3.1)
        if budget.breached():
            log.emit("kernel", "system.halt", {"v": 1, "reason": budget.reason})
            for a in agents: a.stop()
            break
        await asyncio.sleep(cfg.tick_s)
    return summarize(cfg.run_id)               # projection over the event log
```

Routing is purely the `types` filter — the kernel never inspects event *content* to decide who runs. That invariant is what keeps experiments clean.

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
├── .env.example             # ANTHROPIC_API_KEY / setup-token, DATABASE_URL, embed model + dims
├── pyproject.toml
├── config/
│   └── claude/              # clean, checked-in Claude Code config for agents (NOT your ~/.claude)
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
- `substrate/log.py` + `substrate/mcp_server.py`: Postgres event log with `emit_event` / `read_events`, **single-writer (monotonic visibility, §4)**, **self-exclusion**, and **server-side identity**.
- `agent/poll_loop.py` + `agent/runtimes/claude_code.py`: harness-driven loop (§6); one headless Claude Code session; **emit `agent.step` with `saw_events` + `usage` from day one (§4)**.
- `observability/tracing.py`: Langfuse tracing on every model + tool call.
- **Exit criterion:** `docker compose up`, one agent completes a real task, every step is visible in Langfuse and the `events` table, **and the episode replays from the log**.

### Phase 1 — two agents, one topology (days 4–7)
- `kernel/orchestrator.py`: spawn loops + rails, with **quiescence that excludes mid-step agents and a per-session `max_turns` rail (§3.1)**.
- `topologies/supervisor.yaml`: supervisor + worker on one task, coordinating only through the log.
- Exercise the **claim protocol** with two workers of the same role — confirm lowest-id-wins prevents duplicate work (§4).
- **Exit criterion:** the agents solve a task with zero direct calls between them; the log fully explains **and replays** the episode.

### Phase 2 — prove the substrate: heterogeneity, KB, second topology (week 2)
- Add a second runtime: `agent/runtimes/llm.py` + a `FunctionAgent` trigger — ship the **Gmail inbox summarizer** as the first non-Claude-Code agent. Proves the runtime abstraction.
- Add the **human-in-the-loop agent (§3.7)** — your delivery + approval channel: the Gmail summary lands here and `kb.suggestion`s route to you for review.
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
      - ./config/claude:/root/.claude   # CLEAN, checked-in CLI config — NOT your personal ~/.claude
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
- **Auth: don't rely on mounting `~/.claude`.** On macOS Claude Code keeps credentials in the **keychain**, not files, so a `~/.claude` mount can come up unauthenticated. Authenticate the container with `ANTHROPIC_API_KEY` or a long-lived token from `claude setup-token`. Also, mounting your *personal* `~/.claude` leaks your global `CLAUDE.md`, skills, and MCP config into every experiment — an uncontrolled variable; give agents a **clean, checked-in config dir** instead. Bake the CLI into the `Dockerfile`.
- **Domain MCPs (Gmail, etc.)** run either as sidecar services in the same compose file or via the CLI's MCP config; agents reference them by name in their role's `tools`.
- **Secrets via `.env`, never committed.** Ship `.env.example` with the keys blank.
- **One embedding model choice fixes `VECTOR(dims)`** in the KB schema — pick it before first ingest, or you'll be re-embedding.
- **`depends_on` isn't "wait until ready".** Add a small wait-for-Postgres retry in the kernel's startup, or a healthcheck on `db`.

---

*End of sketch — v0.2.*