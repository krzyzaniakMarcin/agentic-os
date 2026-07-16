"""kernel/rate_limit.py — fleet-wide rate-limit resume-gate (phase1-plan §T15).

When a runtime hits a usage limit it raises RateLimitError(wait_s) up to its poll
loop, which closes this shared gate: every loop awaits gate.wait() at the top of
its tick, so the WHOLE FLEET pauses. A single background task reopens the gate
after wait_s. paused_total_s() lets the wall-clock rail (T12 Budget) exclude
throttled time so a reset doesn't masquerade as a runaway.
"""
import asyncio
import time
from collections.abc import Awaitable, Callable

# ponytail: whole-fleet pause — one gate for every agent. Upgrade to per-limit-key
# (per-account) gates only if agents ever span multiple API keys (arch §9).

_MAX_WAIT_S = 3600.0  # never hold the fleet longer than this on a bad reset value


class ResumeGate:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self._clock = clock
        self._sleep = sleep
        self._open = asyncio.Event()
        self._open.set()  # open by default: agents proceed
        self._reopen_at = 0.0
        self._paused_since: float | None = None
        self._paused_total = 0.0
        self._reopen_task: asyncio.Task | None = None

    async def wait(self) -> None:
        """Block at the top of a tick while the fleet is paused."""
        await self._open.wait()

    def close(self) -> None:
        """Teardown: cancel a pending reopen and open the gate so any loop blocked
        in wait() wakes to observe stop() and exit — otherwise a kill-during-pause
        leaves the reopen task sleeping (up to _MAX_WAIT_S) and drains slowly (T15)."""
        if self._reopen_task is not None:
            self._reopen_task.cancel()
        self._open.set()

    @property
    def paused(self) -> bool:
        return not self._open.is_set()

    def paused_total_s(self) -> float:
        """Total seconds the fleet has spent paused, including any current pause."""
        current = (self._clock() - self._paused_since
                   if self._paused_since is not None else 0.0)
        return self._paused_total + current

    def pause(self, wait_s: float) -> None:
        """Close the gate for wait_s seconds. Re-entrant: a second limit hit while
        already paused extends the reopen deadline to the later of the two."""
        deadline = self._clock() + min(max(0.0, wait_s), _MAX_WAIT_S)
        if self.paused:
            self._reopen_at = max(self._reopen_at, deadline)  # running task re-checks
            return
        self._reopen_at = deadline
        self._paused_since = self._clock()
        self._open.clear()
        self._reopen_task = asyncio.create_task(self._reopen())

    async def _reopen(self) -> None:
        try:
            while (remaining := self._reopen_at - self._clock()) > 0:
                await self._sleep(remaining)  # deadline may extend across iterations
        finally:
            # Always release the fleet, even on cancellation/error — a stuck-closed
            # gate would hang every loop forever (T15).
            self._paused_total += self._clock() - self._paused_since
            self._paused_since = None
            self._open.set()
