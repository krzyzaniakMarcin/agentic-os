"""kernel/orchestrator.py — the dumb kernel (phase1-plan §T10).

run_episode(cfg): emit run.start + one seed task.created, spawn one poll loop
per role, then supervise RAILS ONLY — a budget breach becomes system.halt +
stop; run.complete OR quiescence (kernel/termination.py, T11) stops the fleet.
Returns a projection over the event log. Absorbs scripts/run_phase0.py: this
module is the docker-compose `kernel` command (`python -m kernel.orchestrator`).

The kernel never inspects event content or decides who works next — routing is
purely the `types` filter each loop carries (arch §6). Rails, nothing more.
"""
import asyncio
import contextlib
import os
import sys
import uuid

from agent import poll_loop
from agent.role import load_role
from agent.runtimes.claude_code import ClaudeCodeAgent
from kernel import budget as budget_mod
from kernel import config
from kernel import termination
from observability import tracing
from substrate import log

# Default wall-clock cap handed to budget.Budget when the run config omits one —
# a wedged live model can't hang the demo. T15 makes Budget exclude paused time.
_DEFAULT_RUN_TIMEOUT_S = 300.0
# Let the in-flight step() return so the loop records its agent.step before we
# tear down — the exit criterion wants that record (P0 drain discipline).
_DRAIN_TIMEOUT_S = 30.0
# Backstop when no agent emits run.complete: end the run after this much idle
# time (no events, nobody mid-step, fleet not paused). T14 configs may override.
_DEFAULT_QUIESCENCE_S = 30.0


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
        except Exception:
            # ponytail: a crashed poll loop must not abort teardown or lose the
            # run's projection — swallow so summarize() still returns. Surface it
            # as an error event once the kernel has a failure channel (T11+).
            pass


async def run_episode(cfg: dict, *, run_id: str | None = None) -> list[dict]:
    run_id = run_id or uuid.uuid4().hex
    goal = cfg["goal"]
    await log.emit("kernel", "run.start", {"goal": goal}, run_id=run_id)
    await log.emit("kernel", "task.created", {"goal": goal}, run_id=run_id)

    agents = [ClaudeCodeAgent(load_role(r), run_id) for r in cfg["roles"]]
    tasks = [asyncio.create_task(poll_loop.run_agent(a)) for a in agents]
    term = termination.Terminator(
        run_id, agents, cfg.get("quiescence_s", _DEFAULT_QUIESCENCE_S)
    )

    # Injectable for tests; otherwise the real rail (usd + wall-clock + kill).
    budget = cfg.get("budget") or budget_mod.Budget(
        run_id,
        usd_budget=cfg.get("usd_budget"),
        timeout_s=cfg.get("run_timeout_s", _DEFAULT_RUN_TIMEOUT_S),
    )
    tick_s = cfg.get("tick_s", 0.5)
    try:
        while True:
            if reason := await budget.breached():
                await log.emit("kernel", "system.halt", {"reason": reason}, run_id=run_id)
                break
            if await term.terminated():  # run.complete or quiescence (T11)
                break
            await asyncio.sleep(tick_s)
    finally:
        for a in agents:
            a.stop()  # loops exit once their in-flight step() finishes + records agent.step
        await _drain(tasks)
    return await summarize(run_id)


# --- docker `kernel` command: load the YAML topology (phase1-plan §T14) --------

from pathlib import Path  # noqa: E402  (kept local to the entry-point section)

_DEFAULT_TOPOLOGY = Path(__file__).resolve().parent.parent / "topologies" / "supervisor.yaml"


def _topology_path() -> Path:
    """Run-config path via arg or env, else the default topology (phase1-plan
    §T10: 'run-config path via arg/env')."""
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    return Path(os.environ.get("TOPOLOGY", _DEFAULT_TOPOLOGY))


async def main(topology_path: Path | None = None) -> None:
    # topology_path resolved by the caller (the __main__ guard below reads
    # argv/env there) — main() must not read live process sys.argv itself,
    # since that also fires when a test calls main() directly under pytest,
    # whose own argv (test path, -v, ...) would get mistaken for a topology.
    tracing.configure_tracing()  # first live OTLP export call site (absorbs run_phase0)
    cfg = config.load_run_config(topology_path or _DEFAULT_TOPOLOGY)
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
    asyncio.run(main(_topology_path()))
