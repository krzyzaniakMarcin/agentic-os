"""T4 check: harness-driven poll loop, role/step contract (arch §6)."""
import pytest

from agent.base import Agent, Emit
from agent.role import Role, load_role


def test_role_defaults():
    r = Role(name="worker", subscribes_to=["task.created"])
    assert r.emits == []
    assert r.see_own_events is False
    assert r.tick_s == 0.5
    assert r.prompt == ""


def test_load_role_ignores_unknown_keys():
    r = load_role(
        {
            "name": "critic",
            "subscribes_to": ["claim.made"],
            "emits": ["critique.made", "run.complete"],
            "see_own_events": True,
            "extra_field": "ignored",  # config may carry fields Role doesn't model
        }
    )
    assert r.name == "critic"
    assert r.subscribes_to == ["claim.made"]
    assert r.emits == ["critique.made", "run.complete"]
    assert r.see_own_events is True


def test_agent_copies_role_state():
    a = Agent(Role(name="w", subscribes_to=["t"], tick_s=0.1), run_id="r1")
    assert a.name == "w"
    assert a.run_id == "r1"
    assert a.subscribes_to == ["t"]
    assert a.see_own_events is False
    assert a.tick_s == 0.1
    assert a.step_n == 0
    assert a.stopped is False


def test_agent_stop():
    a = Agent(Role(name="w", subscribes_to=["t"]), run_id="r1")
    a.stop()
    assert a.stopped is True


async def test_agent_step_is_abstract():
    a = Agent(Role(name="w", subscribes_to=["t"]), run_id="r1")
    with pytest.raises(NotImplementedError):
        await a.step([])


def test_emit_defaults():
    e = Emit(type="claim.made", payload={"answer": 42})
    assert e.reply_to is None
    assert e.correlation is None


# Task 3: poll_loop tests

import asyncio

from agent import poll_loop
from substrate import log


class _FakeAgent(Agent):
    """Yields one scripted step then stops the loop."""

    def __init__(self, role, run_id, emits):
        super().__init__(role, run_id)
        self._emits = emits
        self.seen = None

    async def step(self, new_events):
        self.seen = new_events
        self.stop()  # loop exits after this iteration
        return self._emits, {"tokens": 7}


async def test_loop_reads_excluding_self_forwards_emits_and_records_step(monkeypatch):
    read_calls = []
    emits = []

    async def fake_read(**kw):
        read_calls.append(kw)
        return [{"id": 10}, {"id": 12}]  # one non-empty batch

    async def fake_emit(agent, type, payload, run_id, reply_to=None, correlation=None):
        emits.append((agent, type, payload, run_id, reply_to, correlation))
        return {"id": len(emits), "ts": 0.0}

    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "emit", fake_emit)

    a = _FakeAgent(
        Role(name="worker", subscribes_to=["task.created"]),
        run_id="r1",
        emits=[Emit("claim.made", {"answer": 42}, correlation="c1")],
    )
    await poll_loop.run_agent(a)

    # Read excluded self and passed subscription + run_id.
    assert read_calls[0]["exclude_agent"] == "worker"
    assert read_calls[0]["run_id"] == "r1"
    assert read_calls[0]["types"] == ["task.created"]
    assert read_calls[0]["since_id"] == 0
    # step() saw the batch.
    assert a.seen == [{"id": 10}, {"id": 12}]
    # The returned emit was forwarded, then agent.step recorded.
    assert emits[0] == ("worker", "claim.made", {"answer": 42}, "r1", None, "c1")
    assert emits[1][1] == "agent.step"
    assert emits[1][2] == {"step_n": 1, "saw_events": [10, 12], "usage": {"tokens": 7}}


async def test_loop_see_own_events_passes_no_exclude(monkeypatch):
    read_calls = []

    async def fake_read(**kw):
        read_calls.append(kw)
        return [{"id": 5}]

    async def fake_emit(*a, **k):
        return {"id": 1, "ts": 0.0}

    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "emit", fake_emit)

    a = _FakeAgent(
        Role(name="solver", subscribes_to=["x"], see_own_events=True),
        run_id="r1",
        emits=[],
    )
    await poll_loop.run_agent(a)
    assert read_calls[0]["exclude_agent"] is None


async def test_loop_sleeps_and_skips_step_when_no_events(monkeypatch):
    stepped = False

    async def fake_read(**kw):
        return []

    async def fake_sleep(_):
        a.stop()  # break out of the idle loop

    class _NoStep(Agent):
        async def step(self, new_events):
            nonlocal stepped
            stepped = True
            return [], {}

    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(poll_loop.asyncio, "sleep", fake_sleep)

    a = _NoStep(Role(name="w", subscribes_to=["x"]), run_id="r1")
    await poll_loop.run_agent(a)
    assert stepped is False  # step() never invoked on an empty read
