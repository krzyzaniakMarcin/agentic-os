"""T11 check: Terminator — run.complete + quiescence with mid-step and
resume-gate guards. In-memory log double + a manual clock, no model/DB."""
from kernel import termination
from substrate import log


class FakeAgent:
    def __init__(self, in_step=False):
        self.in_step = in_step


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


async def test_run_complete_terminates_immediately(monkeypatch):
    _install_log(monkeypatch)
    await log.emit("w", "run.complete", {}, run_id="r")
    term = termination.Terminator("r", [FakeAgent()], quiescence_s=10.0, clock=Clock())
    assert await term.terminated() is True


async def test_quiescence_fires_after_threshold(monkeypatch):
    _install_log(monkeypatch)
    clk = Clock()
    term = termination.Terminator("r", [FakeAgent()], quiescence_s=10.0, clock=clk)
    assert await term.terminated() is False   # t=0, no events yet
    clk.t = 9.0
    assert await term.terminated() is False   # under threshold
    clk.t = 10.0
    assert await term.terminated() is True     # >= quiescence_s of no events


async def test_new_event_resets_quiescence(monkeypatch):
    _install_log(monkeypatch)
    clk = Clock()
    term = termination.Terminator("r", [FakeAgent()], quiescence_s=10.0, clock=clk)
    await term.terminated()                    # baseline at t=0
    clk.t = 9.0
    await log.emit("w", "agent.step", {}, run_id="r")  # activity
    assert await term.terminated() is False    # new event → resets timer
    clk.t = 18.0
    assert await term.terminated() is False    # only 9s since the new event
    clk.t = 19.0
    assert await term.terminated() is True


async def test_in_step_agent_suppresses_quiescence(monkeypatch):
    _install_log(monkeypatch)
    clk = Clock()
    agent = FakeAgent(in_step=True)
    term = termination.Terminator("r", [agent], quiescence_s=10.0, clock=clk)
    await term.terminated()
    clk.t = 100.0
    assert await term.terminated() is False    # mid-step → never quiescent
    agent.in_step = False
    assert await term.terminated() is False    # timer only starts now
    clk.t = 110.0
    assert await term.terminated() is True


async def test_closed_resume_gate_suppresses_quiescence(monkeypatch):
    _install_log(monkeypatch)
    clk = Clock()
    paused = {"v": True}
    term = termination.Terminator("r", [FakeAgent()], quiescence_s=10.0,
                                  clock=clk, paused=lambda: paused["v"])
    await term.terminated()
    clk.t = 100.0
    assert await term.terminated() is False    # fleet paused → busy
    paused["v"] = False
    assert await term.terminated() is False    # timer starts on unpause
    clk.t = 110.0
    assert await term.terminated() is True
