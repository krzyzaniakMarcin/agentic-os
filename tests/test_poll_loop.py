"""T4 check: harness-driven poll loop, role/step contract (arch §6)."""
import uuid

import pytest

from agent import poll_loop
from agent.base import Agent, Emit
from agent.role import Role, load_role
from substrate import log


@pytest.fixture(autouse=True)
async def _close_pool():
    yield
    await log.close()


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


async def test_loop_advances_cursor_and_step_n_across_iterations(monkeypatch):
    reads = []
    steps = []  # agent.step payloads, in order
    batches = [[{"id": 3}, {"id": 5}], [{"id": 8}]]

    async def fake_read(**kw):
        reads.append(kw)
        i = len(reads) - 1
        return batches[i] if i < len(batches) else []

    async def fake_emit(agent, type, payload, run_id, reply_to=None, correlation=None):
        if type == "agent.step":
            steps.append(payload)
        return {"id": 0, "ts": 0.0}

    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "emit", fake_emit)

    class _TwoStep(Agent):
        async def step(self, new_events):
            if self.step_n >= 1:  # already recorded one step -> stop after this one
                self.stop()
            return [], {"n": self.step_n}

    a = _TwoStep(Role(name="w", subscribes_to=["x"]), run_id="r1")
    await poll_loop.run_agent(a)

    # cursor advanced: second read starts after the first batch's last id
    assert reads[0]["since_id"] == 0
    assert reads[1]["since_id"] == 5
    # step_n progressed 1 -> 2 with the right saw_events windows
    assert [s["step_n"] for s in steps] == [1, 2]
    assert steps[0]["saw_events"] == [3, 5]
    assert steps[1]["saw_events"] == [8, 8]
    assert a.step_n == 2


async def test_loop_records_agent_step_against_real_log():
    rid = f"test-{uuid.uuid4()}"
    # Another agent seeds a subscribed event.
    seed = await log.emit("kernel", "task.created", {"goal": "answer"}, run_id=rid)

    class _OneStep(Agent):
        async def step(self, new_events):
            self.stop()
            return [Emit("claim.made", {"answer": 42})], {"tokens": 3}

    a = _OneStep(Role(name="worker", subscribes_to=["task.created"]), run_id=rid)
    await poll_loop.run_agent(a)

    rows = await log.read_events(run_id=rid)
    by_type = {r["type"]: r for r in rows}
    assert by_type["claim.made"]["agent"] == "worker"
    assert by_type["claim.made"]["payload"] == {"v": 1, "answer": 42}
    step = by_type["agent.step"]
    assert step["agent"] == "worker"
    assert step["payload"]["saw_events"] == [seed["id"], seed["id"]]
    assert step["payload"]["step_n"] == 1
    assert step["payload"]["usage"] == {"tokens": 3}


def _capture_step_spans(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from observability import tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("test"))
    return exporter


async def test_loop_emits_step_span_with_usage(monkeypatch):
    exporter = _capture_step_spans(monkeypatch)

    async def fake_read(**kw):
        return [{"id": 10}, {"id": 12}]

    async def fake_emit(*a, **k):
        return {"id": 1, "ts": 0.0}

    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "emit", fake_emit)

    a = _FakeAgent(Role(name="worker", subscribes_to=["task.created"]),
                   run_id="r1", emits=[])
    await poll_loop.run_agent(a)

    (rec,) = exporter.get_finished_spans()
    assert rec.name == "agent.step"
    assert rec.attributes["agent"] == "worker"
    assert rec.attributes["step_n"] == 1
    assert list(rec.attributes["saw_events"]) == [10, 12]
    assert rec.attributes["usage.tokens"] == 7  # _FakeAgent returns {"tokens": 7}
    assert rec.attributes["langfuse.observation.output"] == "[]"  # emits=[] -> empty array


async def test_loop_step_span_records_emitted_output(monkeypatch):
    import json

    exporter = _capture_step_spans(monkeypatch)

    async def fake_read(**kw):
        return [{"id": 10}, {"id": 12}]

    async def fake_emit(*a, **k):
        return {"id": 1, "ts": 0.0}

    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "emit", fake_emit)

    # A runtime that returns real emits (Emit dataclasses) must serialize cleanly.
    a = _FakeAgent(Role(name="worker", subscribes_to=["task.created"]),
                   run_id="r1", emits=[Emit("claim.made", {"answer": 42})])
    await poll_loop.run_agent(a)

    (rec,) = exporter.get_finished_spans()
    assert json.loads(rec.attributes["langfuse.observation.output"]) == [
        {"type": "claim.made", "payload": {"answer": 42},
         "reply_to": None, "correlation": None}
    ]


async def test_in_step_flag_set_during_step(monkeypatch):
    # in-memory log with one subscribed event so step() runs exactly once
    store = [{"id": 1, "run_id": "r", "agent": "seed", "type": "task.created", "payload": {}}]

    async def fake_read(run_id, since_id=0, types=None, correlation=None, limit=50, exclude_agent=None):
        return [e for e in store if e["id"] > since_id and (types is None or e["type"] in types)][:limit]

    async def fake_emit(agent, type, payload, run_id, reply_to=None, correlation=None):
        return {"id": 99, "ts": 0.0}

    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "emit", fake_emit)

    class Probe(Agent):
        seen_in_step = None
        async def step(self, new_events):
            Probe.seen_in_step = self.in_step  # must be True mid-step
            self.stop()
            return [], {}

    role = Role(name="w", subscribes_to=["task.created"], prompt="p")
    agent = Probe(role, "r")
    await poll_loop.run_agent(agent)

    assert Probe.seen_in_step is True
    assert agent.in_step is False  # cleared after the step
