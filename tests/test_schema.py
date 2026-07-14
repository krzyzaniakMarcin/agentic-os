"""T1 check: the events table + indexes match arch §4.

Requires the db container up (`docker compose up -d db`). Point at it with
DATABASE_URL, or set POSTGRES_PASSWORD (+ optionally POSTGRES_DB) and it
defaults to the compose-mapped localhost port.
"""
import os

import asyncpg
import pytest

DSN = os.environ.get("DATABASE_URL") or (
    f"postgresql://postgres:{os.environ.get('POSTGRES_PASSWORD', '')}"
    f"@localhost:5432/{os.environ.get('POSTGRES_DB', 'agentic_os')}"
)

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
EXPECTED_INDEXES = {
    "events_pkey",
    "idx_events_run",
    "idx_events_type",
    "idx_events_corr",
    "idx_events_payload",
}


async def _columns():
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'events'"
        )
        return {r["column_name"]: r["data_type"] for r in rows}
    finally:
        await conn.close()


async def _indexes():
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'events'"
        )
        return {r["indexname"] for r in rows}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_events_columns():
    assert await _columns() == EXPECTED_COLUMNS


@pytest.mark.asyncio
async def test_events_indexes():
    assert EXPECTED_INDEXES <= await _indexes()


if __name__ == "__main__":  # runnable without pytest: `python tests/test_schema.py`
    import asyncio

    async def _main():
        assert await _columns() == EXPECTED_COLUMNS, "events columns mismatch"
        assert EXPECTED_INDEXES <= await _indexes(), "events indexes missing"
        print("ok: events schema matches arch §4")

    asyncio.run(_main())
