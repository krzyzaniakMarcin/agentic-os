"""Append-only event log core (arch §4).

Monotonic-visibility invariant: every append takes a transaction-level
advisory lock around the insert, so commits serialize across ANY number of
writer connections/processes — a reader never sees id N after an event with a
higher id was already read. At Phase 0 throughput the lock is free.
"""
import asyncio
import os

import asyncpg

_ENVELOPE_V = 1
# ponytail: single global append lock — strictly correct (serializes all
# appends). Per-run lock keys only if append throughput ever bites.
_APPEND_LOCK_KEY = 0x4147454E54  # "AGENT"

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()

_COLUMNS = "id, run_id, ts, agent, type, payload, reply_to, correlation"


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or (
        f"postgresql://postgres:{os.environ.get('POSTGRES_PASSWORD', '')}"
        f"@localhost:5432/{os.environ.get('POSTGRES_DB', 'agentic_os')}"
    )


async def _init_conn(conn: asyncpg.Connection) -> None:
    # dict <-> jsonb both directions, so payloads are plain dicts to callers.
    import json

    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _pool_lock:  # double-checked: concurrent first-callers must not race
            if _pool is None:
                _pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=10, init=_init_conn)
    return _pool


async def close() -> None:
    global _pool, _pool_lock
    if _pool is not None:
        await _pool.close()
        _pool = None
    _pool_lock = asyncio.Lock()  # fresh lock: old one may be bound to a dead event loop


async def emit(agent, type, payload, run_id, reply_to=None, correlation=None) -> dict:
    envelope = {**payload, "v": _ENVELOPE_V}
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _APPEND_LOCK_KEY)
            row = await conn.fetchrow(
                "INSERT INTO events (run_id, agent, type, payload, reply_to, correlation) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "RETURNING id, extract(epoch FROM ts) AS ts",
                run_id, agent, type, envelope, reply_to, correlation,
            )
    return {"id": row["id"], "ts": float(row["ts"])}


def _glob_to_like(glob: str) -> str:
    out = []
    for ch in glob:
        if ch == "*":
            out.append("%")
        elif ch == "?":
            out.append("_")
        elif ch in "%_\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


async def read_events(
    run_id, since_id=0, types=None, correlation=None, limit=50, exclude_agent=None
) -> list[dict]:
    conds = ["run_id = $1", "id > $2"]
    args = [run_id, since_id]
    if types:
        args.append([_glob_to_like(t) for t in types])
        conds.append(f"type LIKE ANY(${len(args)}::text[])")
    if correlation is not None:
        args.append(correlation)
        conds.append(f"correlation = ${len(args)}")
    if exclude_agent is not None:
        args.append(exclude_agent)
        conds.append(f"agent <> ${len(args)}")
    args.append(limit)
    q = (
        f"SELECT {_COLUMNS} FROM events WHERE {' AND '.join(conds)} "
        f"ORDER BY id ASC LIMIT ${len(args)}"
    )
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(q, *args)
    return [dict(r) for r in rows]


if __name__ == "__main__":  # smoke: python -m substrate.log
    import asyncio

    async def _main():
        rid = "smoke"
        a = await emit("kernel", "run.start", {"x": 1}, run_id=rid)
        rows = await read_events(run_id=rid, since_id=a["id"] - 1)
        assert rows and rows[-1]["payload"]["v"] == 1
        await close()
        print("ok: log emit/read")

    asyncio.run(_main())
