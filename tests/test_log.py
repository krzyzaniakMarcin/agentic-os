"""T2 check: append-only log core — monotonic visibility + glob filtering (arch §4)."""
import asyncio
import os
import uuid

import pytest

from substrate import log


def _run_id() -> str:
    return f"test-{uuid.uuid4()}"


@pytest.fixture(autouse=True)
async def _close_pool():
    yield
    await log.close()


async def test_emit_returns_id_and_stamps_envelope():
    rid = _run_id()
    r = await log.emit("kernel", "run.start", {"cfg": 1}, run_id=rid)
    assert isinstance(r["id"], int) and r["id"] > 0
    assert isinstance(r["ts"], float)
    rows = await log.read_events(run_id=rid)
    assert len(rows) == 1
    assert rows[0]["payload"] == {"v": 1, "cfg": 1}
    assert rows[0]["agent"] == "kernel"


async def test_monotonic_visibility_under_concurrent_writers():
    # Two connections emit concurrently; a reader advancing its cursor must
    # see EVERY committed id in strictly increasing order — no lost lower id
    # committing after a higher one was already read (the advisory lock).
    rid = _run_id()
    n = 40

    async def writer(tag):
        for i in range(n):
            await log.emit(tag, "claim.made", {"i": i}, run_id=rid)

    seen: list[int] = []
    stop = asyncio.Event()

    async def reader():
        cursor = 0
        while not stop.is_set() or True:
            rows = await log.read_events(run_id=rid, since_id=cursor, limit=1000)
            for row in rows:
                assert row["id"] > cursor        # strictly increasing across reads
                seen.append(row["id"])
                cursor = row["id"]
            if stop.is_set() and not rows:
                return
            await asyncio.sleep(0.001)

    r = asyncio.create_task(reader())
    await asyncio.gather(writer("a"), writer("b"))
    stop.set()
    await r

    assert seen == sorted(seen)                  # monotonic
    assert len(seen) == 2 * n                    # nothing lost


async def test_glob_type_filtering():
    rid = _run_id()
    for t in ("claim.made", "claim.rejected", "critique.made"):
        await log.emit("a", t, {}, run_id=rid)

    def types(rows):
        return sorted(r["type"] for r in rows)

    assert types(await log.read_events(run_id=rid, types=["claim.*"])) == ["claim.made", "claim.rejected"]
    assert types(await log.read_events(run_id=rid, types=["*.made"])) == ["claim.made", "critique.made"]
    assert types(await log.read_events(run_id=rid, types=["critique.made"])) == ["critique.made"]


async def test_exclude_agent_and_correlation():
    rid = _run_id()
    await log.emit("a", "claim.made", {}, run_id=rid, correlation="task-1")
    await log.emit("b", "claim.made", {}, run_id=rid, correlation="task-2")
    excl = await log.read_events(run_id=rid, exclude_agent="a")
    assert [r["agent"] for r in excl] == ["b"]
    corr = await log.read_events(run_id=rid, correlation="task-1")
    assert [r["correlation"] for r in corr] == ["task-1"]
