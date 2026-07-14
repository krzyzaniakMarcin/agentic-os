# T1 — Substrate Scaffolding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `docker compose up` brings up a healthy Postgres (`pgvector`) with the append-only `events` table auto-applied, plus a buildable `kernel` container (Python + Claude Code CLI) that waits for the DB — the substrate floor every later Phase 0 task builds on.

**Architecture:** One `docker-compose.yml` with two services now (`db`, `kernel`) and a comment anchor where the Langfuse stack (new task T8) slots in additively. Schema is auto-applied from `sql/init/` on first DB boot (Postgres entrypoint convention). Only the `events` table this phase — memory/kb DDL is deferred to the phases that use them (arch §4, phase0-plan T1). Async access uses `asyncpg`.

**Tech Stack:** Docker Compose, `pgvector/pgvector:pg16`, Python 3.12, `asyncpg`, Claude Code CLI, Node 20 (CLI runtime).

## Global Constraints

- Append-only `events` table only this phase — no memory/kb DDL (phase0-plan T1).
- Postgres driver is `asyncpg` (locked).
- Secrets via `.env`, never committed; ship `.env.example` with keys blank (arch §12).
- Kernel container command is a placeholder (`sleep infinity`) — neither `orchestrator.py` (P1) nor `scripts/run_phase0.py` (T7) exists yet; T7 swaps it in.
- DB readiness is a healthcheck + `depends_on: condition: service_healthy` — `depends_on` alone ≠ "ready" (arch §12).
- Langfuse self-host is a separate new task **T8** (see phase0-plan.md); T1 only wires its env vars into `.env.example`.

---

### Task 1: Substrate scaffolding (db + kernel + events schema)

**Files:**
- Create: `docker-compose.yml`
- Create: `sql/init/01_events.sql`
- Create: `.env.example`
- Create: `Dockerfile`
- Create: `pyproject.toml`
- Create: `.dockerignore`
- Test: `tests/test_schema.py`

**Interfaces:**
- Produces (consumed by T2 `substrate/log.py`): a running Postgres reachable at `DATABASE_URL` with table `events(id BIGSERIAL PK, run_id TEXT, ts TIMESTAMPTZ, agent TEXT, type TEXT, payload JSONB, reply_to BIGINT, correlation TEXT)` and indexes `idx_events_run/type/corr` + gin on payload.
- Produces (consumed by T7): a `kernel` service with `./` mounted at `/app`, `.env` loaded, `claude` CLI on PATH, whose `command` T7 replaces with `python scripts/run_phase0.py`.

- [ ] **Step 1: `sql/init/01_events.sql`** — events table + indexes verbatim from arch §4 (events only).

```sql
-- Append-only event log. Never UPDATE/DELETE — corrections are new events (arch §4).
CREATE TABLE events (
    id          BIGSERIAL   PRIMARY KEY,             -- global monotonic order
    run_id      TEXT        NOT NULL,                -- which episode
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),  -- emit time
    agent       TEXT        NOT NULL,                -- emitter ("kernel" for system events)
    type        TEXT        NOT NULL,                -- dotted namespace, e.g. "claim.made"
    payload     JSONB       NOT NULL,                -- structured payload (queryable)
    reply_to    BIGINT,                              -- optional: id of event this responds to
    correlation TEXT                                 -- optional: thread/task id
);

CREATE INDEX idx_events_run     ON events(run_id, id);
CREATE INDEX idx_events_type    ON events(run_id, type);
CREATE INDEX idx_events_corr    ON events(run_id, correlation);
CREATE INDEX idx_events_payload ON events USING gin (payload);
```

- [ ] **Step 2: `pyproject.toml`** — minimal metadata + `asyncpg`. Later tasks append their deps (mcp in T3, opentelemetry in T6).

```toml
[project]
name = "agentic-os"
version = "0.0.0"
description = "Local multi-agent coordination substrate"
requires-python = ">=3.12"
dependencies = [
    "asyncpg>=0.29",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]
```

- [ ] **Step 3: `Dockerfile`** — Python base + Node 20 (for the CLI) + install project + Claude Code CLI.

```dockerfile
FROM python:3.12-slim

# Node 20 for the Claude Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Source is bind-mounted at runtime (see docker-compose.yml). command is set there.
```

- [ ] **Step 4: `.dockerignore`** — keep build context lean.

```
.git
data/
docs/
graphify-out/
__pycache__/
*.pyc
.env
```

- [ ] **Step 5: `.env.example`** — blank secrets; Phase 0 vars only. Langfuse vars point at the local self-hosted stack that T8 adds.

```dotenv
# Postgres
POSTGRES_DB=agentic_os
POSTGRES_PASSWORD=
# asyncpg URL used by substrate/log.py (host "db" = compose service name)
DATABASE_URL=postgresql://postgres:@db:5432/agentic_os

# Claude Code CLI auth (API key or `claude setup-token` long-lived token)
ANTHROPIC_API_KEY=

# Langfuse (self-hosted via compose — see phase0-plan.md T8). Points at the
# langfuse-web service added by T8; harmless if unset until T6 wires tracing.
LANGFUSE_HOST=http://langfuse-web:3000
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
```

- [ ] **Step 6: `docker-compose.yml`** — `db` (healthcheck + init mount) + `kernel` (build, waits for healthy db).

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-agentic_os}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./sql/init:/docker-entrypoint-initdb.d   # schema auto-applied on first boot
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d ${POSTGRES_DB:-agentic_os}"]
      interval: 3s
      timeout: 3s
      retries: 10

  kernel:
    build: .
    depends_on:
      db:
        condition: service_healthy      # depends_on alone ≠ "ready" (arch §12)
    env_file: .env
    volumes:
      - ./:/app
      - ./config/claude:/root/.claude   # clean checked-in CLI config, NOT personal ~/.claude
    # T7 swaps this for: python scripts/run_phase0.py
    command: ["sleep", "infinity"]

  # T8 (see phase0-plan.md) adds the Langfuse self-host stack here:
  # clickhouse, redis, minio, langfuse-web, langfuse-worker.

volumes:
  pgdata: {}
```

- [ ] **Step 7: `tests/test_schema.py`** — the one real check: bring the DB up, assert the schema matches §4.

```python
"""T1 check: the events table + indexes are what arch §4 specifies.
Requires the db container up (`docker compose up -d db`) and DATABASE_URL set,
defaulting to the compose-mapped localhost port."""
import os
import asyncio
import asyncpg
import pytest

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:@localhost:5432/agentic_os")

EXPECTED_COLUMNS = {
    "id": "bigint",
    "run_id": "text",
    "ts": "timestamp with time zone",
    "agent": "text",
    "type": "text",
    "payload": "jsonb",
    "reply_to": "bigint",
    "correlation": "text",
}
EXPECTED_INDEXES = {"events_pkey", "idx_events_run", "idx_events_type",
                    "idx_events_corr", "idx_events_payload"}


@pytest.mark.asyncio
async def test_events_columns():
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'events'")
        got = {r["column_name"]: r["data_type"] for r in rows}
    finally:
        await conn.close()
    assert got == EXPECTED_COLUMNS


@pytest.mark.asyncio
async def test_events_indexes():
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'events'")
        got = {r["indexname"] for r in rows}
    finally:
        await conn.close()
    assert EXPECTED_INDEXES <= got


if __name__ == "__main__":  # runnable without pytest: `python tests/test_schema.py`
    asyncio.run(test_events_columns.__wrapped__() if hasattr(test_events_columns, "__wrapped__") else test_events_columns())
```

- [ ] **Step 8: Verify** — `docker compose up -d db`, wait healthy, run the check.

```bash
docker compose up -d db
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q db)")" = healthy ]; do sleep 1; done
pip install -e '.[dev]'
DATABASE_URL=postgresql://postgres:@localhost:5432/agentic_os pytest tests/test_schema.py -v
```
Expected: 2 passed. Then `docker compose build kernel` succeeds and `docker compose up -d` leaves both services running.

- [ ] **Step 9: Commit.**

```bash
git add docker-compose.yml sql/ .env.example Dockerfile pyproject.toml .dockerignore tests/
git commit -m "feat(substrate): T1 scaffolding — db+events schema, kernel image, compose"
```

---

## New task added: T8 — Langfuse self-host in compose

Per the user's decision to self-host Langfuse in this compose (rather than Cloud / an already-running instance), the ~5-container Langfuse stack is tracked as a new task **T8** appended to `phase0-plan.md`, kept out of T1 so the substrate floor stays lean. `.env.example` already carries `LANGFUSE_*` so T6 tracing is unblocked once T8 lands.
