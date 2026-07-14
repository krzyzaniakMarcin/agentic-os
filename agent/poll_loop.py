"""The harness drives the loop, not the model (arch §6). One shared loop for
every runtime: cursor per agent, read new subscribed events (self excluded
unless see_own_events), invoke step() only when there's something to react to,
forward emits, and record one agent.step (saw_events + usage) for replay/cost.
While idle it costs zero tokens."""
import asyncio

from agent.base import Agent
from substrate import log


async def run_agent(agent: Agent, cursor: int = 0) -> None:
    while not agent.stopped:
        events = await log.read_events(
            run_id=agent.run_id,
            since_id=cursor,
            types=agent.subscribes_to,
            # self-exclusion lives here, never in the MCP tool (arch §5, §6)
            exclude_agent=None if agent.see_own_events else agent.name,
        )
        if not events:
            await asyncio.sleep(agent.tick_s)
            continue
        saw = [events[0]["id"], events[-1]["id"]]
        emitted, usage = await agent.step(events)
        for e in emitted:  # inert for the CC runtime (emits=[]); real for others
            await log.emit(
                agent.name, e.type, e.payload, run_id=agent.run_id,
                reply_to=e.reply_to, correlation=e.correlation,
            )
        agent.step_n += 1
        await log.emit(
            agent.name, "agent.step",
            {"step_n": agent.step_n, "saw_events": saw, "usage": usage},
            run_id=agent.run_id,
        )
        cursor = events[-1]["id"]
