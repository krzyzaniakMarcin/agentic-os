"""kernel/termination.py — quiescence-aware episode termination (phase1-plan
§T11). Terminator.terminated() ends a run on any run.complete OR on quiescence:
no new events for quiescence_s AND no agent mid-step AND the fleet not paused.

Excluding in-flight agents is the whole point — a Claude Code agent can be
minutes into a tool-using turn without emitting; naive "no events for T seconds"
fires mid-thought and truncates the run. A T15 rate-limit pause is likewise not
quiescence: `paused` (a closed resume-gate) counts as busy so a throttled fleet
isn't killed. The kernel owns the sessions, so it reads Agent.in_step directly.
"""
import time
from collections.abc import Callable

from substrate import log


class Terminator:
    def __init__(self, run_id: str, agents: list, quiescence_s: float, *,
                 clock: Callable[[], float] = time.monotonic,
                 paused: Callable[[], bool] | None = None):
        self._run_id = run_id
        self._agents = agents
        self._quiescence_s = quiescence_s
        self._clock = clock
        # T15 wires a real resume-gate here; until then the fleet is never paused.
        self._paused = paused or (lambda: False)
        self._last_id = 0
        self._idle_since = clock()  # when the fleet last went idle-and-free

    async def terminated(self) -> bool:
        # Explicit completion always wins, regardless of activity.
        if await log.read_events(run_id=self._run_id, types=["run.complete"], limit=1):
            return True
        # Cheap probe: any new event since we last looked? (plan §T11 — limit=1)
        new = await log.read_events(run_id=self._run_id, since_id=self._last_id, limit=1)
        now = self._clock()
        if new:
            self._last_id = new[-1]["id"]
            self._idle_since = now
            return False
        # No new events. Busy if any agent is mid-step or the fleet is paused —
        # reset the idle clock so quiescence measures only idle-AND-free time.
        if any(a.in_step for a in self._agents) or self._paused():
            self._idle_since = now
            return False
        return (now - self._idle_since) >= self._quiescence_s
