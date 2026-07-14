# Agentic OS

A personal, local system that automates the owner's workflows **and** doubles as a
research substrate for experimenting with multi-agent coordination. Single trusted
user, single machine — the complexity budget goes into coordination, not into
multi-tenancy or durability.

**Status:** design phase. This repo currently holds the architecture and the
Phase 0 implementation plan; no code yet.

## Core ideas

- **Agents talk only through an append-only event log** — no direct agent-to-agent
  RPC. The log is both the message bus and the blackboard.
- **The kernel is deliberately dumb.** Intelligence lives in agents and the
  coordination protocol, never in the kernel.
- **Topology is data, not code** — supervisor / debate / blackboard / market are
  role prompts + subscription rules, not kernel branches.
- **One command up:** `docker compose up`. Postgres + `pgvector` is the single
  datastore (event log, memory, knowledge base); git for artifacts.

## Docs

- [`docs/agentic-os-architecture.md`](docs/agentic-os-architecture.md) — architecture sketch (draft v0.2)
- [`docs/phase0-plan.md`](docs/phase0-plan.md) — Phase 0 plan: one agent, end to end

## Phase 0 goal

`docker compose up` → one Claude Code agent completes a real task through the
substrate; every step is visible in Langfuse and the `events` table, and the
episode is recorded replayably.
