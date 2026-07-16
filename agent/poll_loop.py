"""The harness drives the loop, not the model (arch §6). One shared loop for
every runtime: cursor per agent, read new subscribed events (self excluded
unless see_own_events), invoke step() only when there's something to react to,
forward emits, and record one agent.step (saw_events + usage) for replay/cost.
While idle it costs zero tokens."""
import asyncio
import json

from agent.base import Agent, RateLimitError
from observability import tracing
from substrate import log


async def run_agent(agent: Agent, gate=None, cursor: int = 0) -> None:
    while not agent.stopped:
        if gate is not None:
            await gate.wait()  # resume-gate: block while the fleet is throttled (T15)
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
        try:
            with tracing.step_span(
                agent.name, agent.run_id, agent.step_n + 1, saw, input_events=events
            ) as span:
                agent.in_step = True
                try:
                    emitted, usage = await agent.step(events)
                finally:
                    agent.in_step = False
                span.set_attributes(tracing.usage_attrs(usage))
                span.set_attributes(tracing.generation_attrs(usage))  # Tokens/Cost columns
                if span.is_recording():  # [] for the CC runtime; real emits for others
                    span.set_attribute("langfuse.observation.output", json.dumps(
                        [{"type": e.type, "payload": e.payload,
                          "reply_to": e.reply_to, "correlation": e.correlation}
                         for e in emitted], default=str))
        except RateLimitError as e:
            # Fleet throttled. Pause everyone and drop this attempt WHOLE: no
            # agent.step, step_n/cursor unchanged, so the same window is
            # re-delivered on resume (phase1-plan §T15). The coroutine stays alive
            # across the pause, so the in-place window lives in this local, not the
            # log — do not advance the cursor here.
            if gate is not None:
                gate.pause(e.wait_s)
            continue
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
