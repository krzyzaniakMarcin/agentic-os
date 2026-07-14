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
