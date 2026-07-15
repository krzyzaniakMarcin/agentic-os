"""T7 — throwaway Phase 0 demo runner (phase0-plan T7).

First LIVE `claude -p` run: seeds one task, drives one Claude Code agent through
the shared poll loop, waits for the model's `run.complete` under a timeout, then
dumps the events table. Validates the CLI + config/claude/.mcp.json + OTel wiring
end to end. Absorbed by orchestrator.py in Phase 1.

Run in-container: `docker compose up` (kernel command is this script). Auth for
the in-container `claude` comes from CLAUDE_CODE_OAUTH_TOKEN in .env (Pro-plan
token from `claude setup-token`).
"""
import asyncio
import contextlib
import uuid

from agent import poll_loop
from agent.role import Role
from agent.runtimes.claude_code import ClaudeCodeAgent
from observability import tracing
from substrate import log

SEED_GOAL = "What is the capital of France?"
RUN_TIMEOUT_S = 120.0
# After run.complete, let the in-flight step() return so the loop records its
# agent.step before we tear down — the exit criterion wants that record.
DRAIN_TIMEOUT_S = 30.0

WORKER_PROMPT = (
    "You are a worker agent in a multi-agent system. When you see a "
    "'task.created' event, answer the question in its payload's 'goal' field. "
    "Emit your answer as a 'claim.made' event with payload {\"answer\": <your "
    "answer>} using the emit_event tool, then emit a 'run.complete' event to "
    "signal the episode is done."
)


async def _wait_for_complete(run_id: str) -> dict:
    """Poll the log until a run.complete event exists; return it."""
    while True:
        done = await log.read_events(run_id=run_id, types=["run.complete"])
        if done:
            return done[0]
        await asyncio.sleep(0.5)


async def _dump_events(run_id: str) -> None:
    """Print the run's full event log — the replayable record (arch §4)."""
    events = await log.read_events(run_id=run_id, limit=500)
    print(f"\n=== events for run {run_id} ({len(events)}) ===")
    for e in events:
        print(f"{e['id']:>4} | {e['agent']:<8} | {e['type']:<14} | {e['payload']}")


async def main() -> None:
    tracing.configure_tracing()  # first live OTLP export call site
    run_id = uuid.uuid4().hex
    print(f"run_id={run_id} goal={SEED_GOAL!r}")

    await log.emit("kernel", "run.start", {"goal": SEED_GOAL}, run_id=run_id)
    await log.emit("kernel", "task.created", {"goal": SEED_GOAL}, run_id=run_id)

    agent = ClaudeCodeAgent(
        Role(name="worker", subscribes_to=["task.created"], prompt=WORKER_PROMPT),
        run_id,
    )
    loop_task = asyncio.create_task(poll_loop.run_agent(agent))
    try:
        await asyncio.wait_for(_wait_for_complete(run_id), timeout=RUN_TIMEOUT_S)
        print("run.complete received")
    except asyncio.TimeoutError:
        print(f"TIMEOUT after {RUN_TIMEOUT_S}s — agent wedged, dumping anyway")
    finally:
        agent.stop()  # loop exits once the in-flight step() finishes + records agent.step
        try:
            await asyncio.wait_for(loop_task, timeout=DRAIN_TIMEOUT_S)
        except asyncio.TimeoutError:  # wedged step — stop waiting and tear down
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
        tracing.shutdown_tracing()  # flush step spans to Langfuse before exit
        await _dump_events(run_id)
        await log.close()


if __name__ == "__main__":
    asyncio.run(main())
