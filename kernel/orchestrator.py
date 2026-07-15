"""kernel/orchestrator.py — the dumb kernel (phase1-plan §T10).

run_episode(cfg): emit run.start + one seed task.created, spawn one poll loop
per role, then supervise RAILS ONLY — a budget breach becomes system.halt +
stop; run.complete (T11 adds quiescence) stops the fleet. Returns a projection
over the event log. Absorbs scripts/run_phase0.py: this module is the
docker-compose `kernel` command (`python -m kernel.orchestrator`).

The kernel never inspects event content or decides who works next — routing is
purely the `types` filter each loop carries (arch §6). Rails, nothing more.
"""
import asyncio
import contextlib
import time
import uuid

from agent import poll_loop
from agent.role import load_role
from agent.runtimes.claude_code import ClaudeCodeAgent
from observability import tracing
from substrate import log

# ponytail: standalone wall-clock guard so a wedged live model can't hang the
# demo before T12 exists. T12 folds this into budget.breached() (usd + wall-
# clock + kill switch), and T15 makes it exclude paused time — the `elapsed`
# calc is factored here so that's a one-line move, not a retrofit.
_DEFAULT_RUN_TIMEOUT_S = 300.0
# Let the in-flight step() return so the loop records its agent.step before we
# tear down — the exit criterion wants that record (P0 drain discipline).
_DRAIN_TIMEOUT_S = 30.0


async def _run_complete(run_id: str) -> bool:
    """T10 termination seam: any run.complete ends the episode. T11 replaces
    this call site with kernel/termination.terminated (adds quiescence + a
    mid-step guard + the T15 resume-gate check)."""
    done = await log.read_events(run_id=run_id, types=["run.complete"], limit=1)
    return bool(done)


async def summarize(run_id: str) -> list[dict]:
    """Projection over the log — the replayable record (arch §4). Same 'dump the
    events table' the P0 runner did, now the kernel's job."""
    return await log.read_events(run_id=run_id, limit=500)


async def _drain(tasks: list[asyncio.Task]) -> None:
    """Agents already stop()'d; await each loop so its in-flight step() records
    agent.step, then cancel any wedged step (P0 drain discipline)."""
    for t in tasks:
        try:
            await asyncio.wait_for(t, timeout=_DRAIN_TIMEOUT_S)
        except asyncio.TimeoutError:  # wedged step — stop waiting and tear down
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t


async def run_episode(cfg: dict, *, run_id: str | None = None) -> list[dict]:
    run_id = run_id or uuid.uuid4().hex
    goal = cfg["goal"]
    await log.emit("kernel", "run.start", {"goal": goal}, run_id=run_id)
    await log.emit("kernel", "task.created", {"goal": goal}, run_id=run_id)

    agents = [ClaudeCodeAgent(load_role(r), run_id) for r in cfg["roles"]]
    tasks = [asyncio.create_task(poll_loop.run_agent(a)) for a in agents]

    budget = cfg.get("budget")  # T12 supplies an object with async breached()->str|None
    tick_s = cfg.get("tick_s", 0.5)
    timeout_s = cfg.get("run_timeout_s", _DEFAULT_RUN_TIMEOUT_S)
    started = time.monotonic()
    try:
        while True:
            if budget is not None and (reason := await budget.breached()):
                await log.emit("kernel", "system.halt", {"reason": reason}, run_id=run_id)
                break
            elapsed = time.monotonic() - started  # T15: subtract paused time here
            if timeout_s is not None and elapsed > timeout_s:
                await log.emit("kernel", "system.halt", {"reason": "timeout"}, run_id=run_id)
                break
            if await _run_complete(run_id):  # T11 swaps in quiescence-aware terminated()
                break
            await asyncio.sleep(tick_s)
    finally:
        for a in agents:
            a.stop()  # loops exit once their in-flight step() finishes + records agent.step
        await _drain(tasks)
    return await summarize(run_id)


# --- docker `kernel` command: hardcoded stub cfg (T14 replaces with YAML) -----

_STUB_GOAL = "What is the capital of France?"
_WORKER_PROMPT = (
    "You are a worker agent in a multi-agent system. When you see a "
    "'task.created' event, answer the question in its payload's 'goal' field. "
    "Emit your answer as a 'claim.made' event with payload {\"answer\": <your "
    "answer>} using the emit_event tool, then emit a 'run.complete' event to "
    "signal the episode is done."
)


def _stub_cfg() -> dict:
    """Hardcoded run config mirroring run_phase0 (phase1-plan §T10). T14 replaces
    this with topologies/supervisor.yaml + a loader. Defines the cfg *shape*
    run_episode consumes; T14 supplies the parser."""
    return {
        "goal": _STUB_GOAL,
        "roles": [
            {"name": "worker", "subscribes_to": ["task.created"], "prompt": _WORKER_PROMPT},
        ],
    }


async def main() -> None:
    tracing.configure_tracing()  # first live OTLP export call site (absorbs run_phase0)
    cfg = _stub_cfg()
    print(f"goal={cfg['goal']!r}")
    try:
        events = await run_episode(cfg)
        print(f"\n=== events ({len(events)}) ===")
        for e in events:
            print(f"{e['id']:>4} | {e['agent']:<8} | {e['type']:<14} | {e['payload']}")
    finally:
        tracing.shutdown_tracing()  # flush step spans to Langfuse before exit
        await log.close()


if __name__ == "__main__":
    asyncio.run(main())
