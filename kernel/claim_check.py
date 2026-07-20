"""The T16 guard: exactly one `claim.made` per `task.assigned` (phase1-plan §T16).

The claim protocol is model-driven (prompt + one ad-hoc MCP read), so the risk
is model reliability and this assertion is what catches a regression. Runs as a
projection over the event log — offline in tests against synthetic events, or
`python -m kernel.claim_check <run_id>` against a real run.

ponytail: the winner rule the protocol implements (topologies/
supervisor_claim.yaml's &claim_prompt) is identity-based and retry-safe: a
step retry re-emits task.claimed at a higher id, but the guard below still
finds the SAME agent at the lowest id, and a retry landing after claim.made
already exists is a no-op by protocol — provided the model follows step 3
(topologies/supervisor_claim.yaml's &claim_prompt), not by any code-level
guarantee. The remaining, genuinely
unrecoverable case is a worker that holds the lowest task.claimed and then
never runs again (dies permanently, no retry) — that strands the task at
zero claim.made for the whole run, and this guard is what catches it, loudly,
as "0 claims" rather than passing silently.
"""
from __future__ import annotations

import asyncio
import sys

from substrate import log


def assert_one_claim_per_task(events: list[dict]) -> None:
    """Raise AssertionError unless every task.assigned has exactly one claim.made
    correlated to it, and no claim.made correlates to anything else. Also
    verifies the claim PROTOCOL was actually followed, not just the outcome:
    each claim.made must be backed by a task.claimed from the same agent at the
    same correlation, and that task.claimed must be the lowest-id one there —
    otherwise a run that skipped claiming entirely could still look green."""
    tasks = {e["id"] for e in events if e["type"] == "task.assigned"}
    claims: dict[str, list[dict]] = {}
    claimed: dict[str, list[dict]] = {}
    for e in events:
        if e["type"] == "claim.made":
            # correlation is TEXT in the log; normalize so an int-carrying
            # replayed/fake event matches the same task.
            claims.setdefault(str(e["correlation"]), []).append(e)
        elif e["type"] == "task.claimed":
            claimed.setdefault(str(e["correlation"]), []).append(e)

    for task_id in sorted(tasks):
        got = claims.get(str(task_id), [])
        assert len(got) == 1, (
            f"task.assigned id={task_id}: {len(got)} claims, expected 1 "
            f"(by {[c['agent'] for c in got]})"
        )
        winner = got[0]
        rivals = claimed.get(str(task_id), [])
        mine = [c for c in rivals if c["agent"] == winner["agent"]]
        assert mine, (
            f"task.assigned id={task_id}: claim.made by {winner['agent']} has no "
            f"matching task.claimed — protocol was skipped"
        )
        lowest = min(rivals, key=lambda c: c["id"])
        assert lowest["agent"] == winner["agent"], (
            f"task.assigned id={task_id}: claim.made by {winner['agent']} but "
            f"the lowest task.claimed id was from {lowest['agent']}"
        )

    orphans = sorted(set(claims) - {str(t) for t in tasks})
    assert not orphans, f"claim.made with no task.assigned for correlation(s) {orphans}"


async def _main(run_id: str) -> None:
    # limit matches kernel/orchestrator.py's summarize() cap; a longer run reads
    # only a prefix here and fails safe (spurious "0 claims"/orphan, never a
    # false green).
    events = await log.read_events(run_id=run_id, limit=500)
    try:
        assert_one_claim_per_task(events)
    finally:
        await log.close()
    print(f"ok: one claim.made per task.assigned across {len(events)} events")


if __name__ == "__main__":  # verify a live run: python -m kernel.claim_check <run_id>
    asyncio.run(_main(sys.argv[1]))
