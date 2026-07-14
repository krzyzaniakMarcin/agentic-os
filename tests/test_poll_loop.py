"""T4 check: harness-driven poll loop, role/step contract (arch §6)."""
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
