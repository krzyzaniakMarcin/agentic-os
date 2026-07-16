"""T15 check: ResumeGate — shared fleet resume-gate. Manual clock/sleep, no I/O
(mirrors tests/test_budget.py's Clock idiom)."""
import asyncio

from kernel.rate_limit import ResumeGate


class _Clock:
    """Monotonic-ish manual clock; its sleep() advances time so a gate's reopen
    loop terminates without real waiting."""
    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


def _gate(clock: _Clock) -> ResumeGate:
    return ResumeGate(clock=clock.now, sleep=clock.sleep)


async def test_gate_open_by_default():
    gate = _gate(_Clock())
    assert gate.paused is False
    await gate.wait()  # returns immediately
    assert gate.paused_total_s() == 0.0


async def test_pause_closes_then_reopens_after_wait():
    clock = _Clock()
    gate = _gate(clock)
    gate.pause(30.0)
    assert gate.paused is True
    await gate.wait()  # reopen task advances the clock, then unblocks
    assert gate.paused is False
    assert gate.paused_total_s() == 30.0


async def test_second_pause_extends_deadline():
    clock = _Clock()
    gate = _gate(clock)
    gate.pause(10.0)
    gate.pause(50.0)  # later deadline wins while already paused
    await gate.wait()
    assert gate.paused_total_s() == 50.0


async def test_pause_wait_is_clamped():
    clock = _Clock()
    gate = _gate(clock)
    gate.pause(10**9)  # absurd reset value must not hang the fleet forever
    await gate.wait()
    assert gate.paused_total_s() == 3600.0


async def test_paused_total_accrues_across_two_pauses():
    clock = _Clock()
    gate = _gate(clock)
    gate.pause(20.0)
    await gate.wait()
    gate.pause(15.0)
    await gate.wait()
    assert gate.paused_total_s() == 35.0


async def test_reopen_cancellation_releases_gate():
    # A clock/sleep that blocks so the reopen task is mid-await when cancelled.
    class _Blocking:
        def __init__(self): self.t = 0.0
        def now(self): return self.t
        async def sleep(self, s): await asyncio.Event().wait()  # never returns
    clk = _Blocking()
    gate = ResumeGate(clock=clk.now, sleep=clk.sleep)
    gate.pause(30.0)
    assert gate.paused is True
    await asyncio.sleep(0)          # let the reopen task start and block on sleep
    gate._reopen_task.cancel()
    try:
        await gate._reopen_task
    except asyncio.CancelledError:
        pass
    assert gate.paused is False     # finally released the gate
