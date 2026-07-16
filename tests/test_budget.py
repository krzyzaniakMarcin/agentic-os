"""T12 check: Budget rail — usd cost cap, wall-clock, and system.kill. In-memory
log double + a manual clock, no model/DB (mirrors tests/test_termination.py)."""
from kernel import budget
from substrate import log


def _install_log(monkeypatch):
    store: list[dict] = []

    async def fake_emit(agent, type, payload, run_id, reply_to=None, correlation=None):
        store.append({"id": len(store) + 1, "agent": agent, "type": type,
                      "payload": payload, "run_id": run_id})
        return {"id": len(store), "ts": 0.0}

    async def fake_read(run_id, since_id=0, types=None, correlation=None,
                        limit=50, exclude_agent=None):
        out = [e for e in store if e["run_id"] == run_id and e["id"] > since_id
               and (types is None or e["type"] in types)]
        return out[:limit]

    monkeypatch.setattr(log, "emit", fake_emit)
    monkeypatch.setattr(log, "read_events", fake_read)
    return store


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


async def _step(cost):
    usage = {} if cost is None else {"total_cost_usd": cost}
    await log.emit("w", "agent.step", {"step_n": 1, "usage": usage}, run_id="r")


async def test_usd_budget_trips_when_running_total_exceeds(monkeypatch):
    _install_log(monkeypatch)
    b = budget.Budget("r", usd_budget=1.0, clock=Clock())
    await _step(0.4)
    assert await b.breached() is None      # 0.4
    await _step(0.4)
    assert await b.breached() is None      # 0.8
    await _step(0.4)
    assert await b.breached() == "usd_budget"  # 1.2 > 1.0


async def test_usd_budget_survives_more_than_50_steps(monkeypatch):
    # read_events caps at limit=50 — a single breached() must drain ALL new
    # agent.step rows, or the total freezes and the rail stops tripping.
    _install_log(monkeypatch)
    b = budget.Budget("r", usd_budget=5.0, clock=Clock())
    for _ in range(60):
        await _step(0.1)               # total 6.0 across 60 events
    assert await b.breached() == "usd_budget"


async def test_kill_switch_trips(monkeypatch):
    _install_log(monkeypatch)
    b = budget.Budget("r", usd_budget=1000.0, timeout_s=1000.0, clock=Clock())
    assert await b.breached() is None
    await log.emit("owner", "system.kill", {}, run_id="r")
    assert await b.breached() == "kill"


async def test_wall_clock_timeout_trips(monkeypatch):
    _install_log(monkeypatch)
    clk = Clock()
    b = budget.Budget("r", timeout_s=10.0, clock=clk)
    clk.t = 9.0
    assert await b.breached() is None      # under threshold
    clk.t = 11.0
    assert await b.breached() == "timeout"


async def test_missing_cost_counts_as_zero(monkeypatch):
    _install_log(monkeypatch)
    b = budget.Budget("r", usd_budget=1.0, clock=Clock())
    await _step(None)                      # usage without total_cost_usd
    await _step(None)
    await log.emit("w", "claim.made", {"answer": "x"}, run_id="r")  # not agent.step
    assert await b.breached() is None      # spent stays 0.0
    await _step(1.5)
    assert await b.breached() == "usd_budget"


async def test_no_rails_never_breaches(monkeypatch):
    _install_log(monkeypatch)
    clk = Clock()
    b = budget.Budget("r", clock=clk)      # usd_budget=None, timeout_s=None
    await _step(999.0)
    clk.t = 10_000.0
    assert await b.breached() is None
