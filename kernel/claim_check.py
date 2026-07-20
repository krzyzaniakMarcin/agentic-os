"""The T16 guard: exactly one `claim.made` per `task.assigned` (phase1-plan §T16).

The claim protocol is model-driven (prompt + one ad-hoc MCP read), so the risk
is model reliability and this assertion is what catches a regression. Runs as a
projection over the event log — offline in tests against synthetic events, or
`python -m kernel.claim_check <run_id>` against a real run.
"""
from __future__ import annotations

import asyncio
import sys

from substrate import log


def assert_one_claim_per_task(events: list[dict]) -> None:
    """Raise AssertionError unless every task.assigned has exactly one claim.made
    correlated to it, and no claim.made correlates to anything else."""
    tasks = {e["id"] for e in events if e["type"] == "task.assigned"}
    claims: dict[str, list[dict]] = {}
    for e in events:
        if e["type"] == "claim.made":
            # correlation is TEXT in the log; normalize so an int-carrying
            # replayed/fake event matches the same task.
            claims.setdefault(str(e["correlation"]), []).append(e)

    for task_id in sorted(tasks):
        got = claims.get(str(task_id), [])
        assert len(got) == 1, (
            f"task.assigned id={task_id}: {len(got)} claims, expected 1 "
            f"(by {[c['agent'] for c in got]})"
        )

    orphans = sorted(set(claims) - {str(t) for t in tasks})
    assert not orphans, f"claim.made with no task.assigned for correlation(s) {orphans}"


async def _main(run_id: str) -> None:
    events = await log.read_events(run_id=run_id, limit=500)
    try:
        assert_one_claim_per_task(events)
    finally:
        await log.close()
    print(f"ok: one claim.made per task.assigned across {len(events)} events")


if __name__ == "__main__":  # verify a live run: python -m kernel.claim_check <run_id>
    asyncio.run(_main(sys.argv[1]))
