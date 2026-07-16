"""kernel/budget.py — the rails (phase1-plan §T12). Budget.breached() returns
the first tripped reason among: a `system.kill` event (owner override, log is
the control channel), a global `usd_budget` summed from agent.step usage, and a
wall-clock `timeout_s` from run.start. The orchestrator (T10) turns a breach
into system.halt + stop.

Cost accumulates incrementally by an agent.step cursor + running total, draining
past read_events' default limit=50 so the total never freezes (plan §T12). The
elapsed calc is one method so T15's "exclude paused time" is a one-line add.
"""
import time
from collections.abc import Callable

from substrate import log


def _step_cost(event: dict) -> float:
    """total_cost_usd for one agent.step; missing/None/non-numeric → 0.0 so a
    malformed step can't crash the rail (plan §T12)."""
    usage = event.get("payload", {}).get("usage") or {}
    cost = usage.get("total_cost_usd")
    return float(cost) if isinstance(cost, (int, float)) else 0.0


class Budget:
    def __init__(self, run_id: str, *, usd_budget: float | None = None,
                 timeout_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._run_id = run_id
        self._usd_budget = usd_budget
        self._timeout_s = timeout_s
        self._clock = clock
        self._started = clock()
        self._since_id = 0      # agent.step cursor — never re-scan
        self._spent = 0.0       # running dollar total

    def _elapsed(self) -> float:
        # T15 subtracts paused time here — factored so it's a one-line change.
        return self._clock() - self._started

    async def _accrue_cost(self) -> None:
        while True:
            rows = await log.read_events(run_id=self._run_id,
                                         since_id=self._since_id,
                                         types=["agent.step"], limit=50)
            if not rows:
                return
            for e in rows:
                self._spent += _step_cost(e)
            self._since_id = rows[-1]["id"]

    async def breached(self) -> str | None:
        # Kill switch: cheapest, immediate — owner override wins.
        if await log.read_events(run_id=self._run_id, types=["system.kill"], limit=1):
            return "kill"
        if self._usd_budget is not None:
            await self._accrue_cost()
            if self._spent > self._usd_budget:
                return "usd_budget"
        if self._timeout_s is not None and self._elapsed() > self._timeout_s:
            return "timeout"
        return None
