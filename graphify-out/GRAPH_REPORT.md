# Graph Report - .  (2026-07-14)

## Corpus Check
- Corpus is ~6,634 words - fits in a single context window. You may not need a graph.

## Summary
- 45 nodes · 67 edges · 7 communities
- Extraction: 90% EXTRACTED · 10% INFERRED · 0% AMBIGUOUS · INFERRED: 7 edges (avg confidence: 0.81)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Agent Contract & MCP Boundary
- Event Log & Ordering Invariants
- Substrate Stores & Human-in-Loop
- Replay & Claude Code Runtime
- Kernel & Deployment
- Coordination Topologies
- Observability

## God Nodes (most connected - your core abstractions)
1. `Append-Only Event Log` - 10 edges
2. `Agent (contract, not runtime)` - 7 edges
3. `Substrate MCP Server (syscall boundary)` - 6 edges
4. `Harness-Driven Poll Loop` - 6 edges
5. `agent.step Event (saw_events + usage)` - 6 edges
6. `Kernel (dumb asyncio orchestrator)` - 5 edges
7. `Postgres + pgvector (single datastore)` - 5 edges
8. `Phase 0 — One Agent End to End` - 5 edges
9. `Shared Substrate` - 4 edges
10. `Monotonic Visibility Invariant` - 4 edges

## Surprising Connections (you probably didn't know these)
- `run_phase0.py (thin throwaway runner)` --semantically_similar_to--> `Kernel (dumb asyncio orchestrator)`  [INFERRED] [semantically similar]
  docs/phase0-plan.md → docs/agentic-os-architecture.md
- `pg_advisory_xact_lock` --semantically_similar_to--> `Single-Writer Serialization`  [INFERRED] [semantically similar]
  docs/phase0-plan.md → docs/agentic-os-architecture.md
- `Pure-Log Demo Task` --references--> `Append-Only Event Log`  [EXTRACTED]
  docs/phase0-plan.md → docs/agentic-os-architecture.md
- `T2: substrate/log.py` --implements--> `Append-Only Event Log`  [EXTRACTED]
  docs/phase0-plan.md → docs/agentic-os-architecture.md
- `Connection Identity (AGENT_NAME / RUN_ID env)` --references--> `Substrate MCP Server (syscall boundary)`  [EXTRACTED]
  docs/phase0-plan.md → docs/agentic-os-architecture.md

## Hyperedges (group relationships)
- **Substrate stores in one Postgres** — docs_agentic_os_architecture_event_log, docs_agentic_os_architecture_agent_memory, docs_agentic_os_architecture_knowledge_base, docs_agentic_os_architecture_postgres_pgvector [EXTRACTED 0.90]
- **Heterogeneous runtime fleet behind one contract** — docs_agentic_os_architecture_claudecodeagent, docs_agentic_os_architecture_llmagent, docs_agentic_os_architecture_functionagent, docs_agentic_os_architecture_foreignagent, docs_agentic_os_architecture_agent_contract [EXTRACTED 0.90]
- **Phase 0 substrate build tasks** — docs_phase0_plan_t2_log, docs_phase0_plan_t3_mcp, docs_phase0_plan_t6_tracing, docs_phase0_plan_phase0 [EXTRACTED 0.85]

## Communities (7 total, 0 thin omitted)

### Community 0 - "Agent Contract & MCP Boundary"
Cohesion: 0.32
Nodes (8): Agent (contract, not runtime), emit_event, ForeignAgent, LLMAgent, read_events, Substrate MCP Server (syscall boundary), Connection Identity (AGENT_NAME / RUN_ID env), T3: substrate/mcp_server.py

### Community 1 - "Event Log & Ordering Invariants"
Cohesion: 0.39
Nodes (8): Claim Protocol (lowest-id-wins), Append-Only Event Log, Monotonic Visibility Invariant, Versioned Payload Envelope, Single-Writer Serialization, pg_advisory_xact_lock, Pure-Log Demo Task, T2: substrate/log.py

### Community 2 - "Substrate Stores & Human-in-Loop"
Cohesion: 0.29
Nodes (7): Agent Memory, Artifact Memory (git repo), Content-Reference Convention, FunctionAgent, Human-in-the-Loop Agent, Knowledge Base (curated), Shared Substrate

### Community 3 - "Replay & Claude Code Runtime"
Cohesion: 0.33
Nodes (7): agent.step Event (saw_events + usage), ClaudeCodeAgent, Rails (budget / wall-clock / kill), Replay from the Log, Record-Only Replay (decision), Stateless per-step claude -p Subprocess, Who-Emits-What Resolution

### Community 4 - "Kernel & Deployment"
Cohesion: 0.43
Nodes (7): Docker Compose (one-command up), Kernel (dumb asyncio orchestrator), Harness-Driven Poll Loop, Postgres + pgvector (single datastore), Quiescence Termination, Phase 0 — One Agent End to End, run_phase0.py (thin throwaway runner)

### Community 5 - "Coordination Topologies"
Cohesion: 0.40
Nodes (5): Debate Topology, Peer Critique Topology, Self-Exclusion (no self-echo), Supervisor/Worker Topology, Topology as Configuration

### Community 6 - "Observability"
Cohesion: 1.00
Nodes (3): Observability (Langfuse via OTel), Claude Code OTel Export, T6: observability/tracing.py

## Knowledge Gaps
- **4 isolated node(s):** `ForeignAgent`, `Supervisor/Worker Topology`, `Peer Critique Topology`, `Quiescence Termination`
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Append-Only Event Log` connect `Event Log & Ordering Invariants` to `Agent Contract & MCP Boundary`, `Substrate Stores & Human-in-Loop`, `Kernel & Deployment`?**
  _High betweenness centrality (0.374) - this node is a cross-community bridge._
- **Why does `Agent (contract, not runtime)` connect `Agent Contract & MCP Boundary` to `Substrate Stores & Human-in-Loop`, `Replay & Claude Code Runtime`, `Kernel & Deployment`, `Coordination Topologies`?**
  _High betweenness centrality (0.316) - this node is a cross-community bridge._
- **Why does `Harness-Driven Poll Loop` connect `Kernel & Deployment` to `Agent Contract & MCP Boundary`, `Replay & Claude Code Runtime`, `Coordination Topologies`?**
  _High betweenness centrality (0.312) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `agent.step Event (saw_events + usage)` (e.g. with `Observability (Langfuse via OTel)` and `Rails (budget / wall-clock / kill)`) actually correct?**
  _`agent.step Event (saw_events + usage)` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `ForeignAgent`, `Supervisor/Worker Topology`, `Peer Critique Topology` to the rest of the system?**
  _4 weakly-connected nodes found - possible documentation gaps or missing edges._