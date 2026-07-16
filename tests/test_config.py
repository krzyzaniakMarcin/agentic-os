from pathlib import Path

from kernel.config import load_run_config

_YAML = """\
goal: "What is the capital of France?"
rails:
  usd_budget: 5.0
  timeout_s: 120
  quiescence_s: 20
roles:
  - name: supervisor
    subscribes_to: [task.created, claim.made]
    prompt: "decompose then aggregate"
  - name: worker
    subscribes_to: [task.assigned]
    prompt: "answer each sub-question"
    runtime: claude_code   # inert in Phase 1 — load_role drops it
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "topo.yaml"
    p.write_text(text)
    return p


def test_loads_goal_roles_and_maps_rails(tmp_path):
    cfg = load_run_config(_write(tmp_path, _YAML))
    assert cfg["goal"] == "What is the capital of France?"
    assert [r["name"] for r in cfg["roles"]] == ["supervisor", "worker"]
    assert cfg["roles"][0]["subscribes_to"] == ["task.created", "claim.made"]
    # rails: timeout_s -> run_timeout_s, others pass through under their own names
    assert cfg["usd_budget"] == 5.0
    assert cfg["run_timeout_s"] == 120
    assert cfg["quiescence_s"] == 20
    assert "timeout_s" not in cfg  # renamed, not duplicated


def test_absent_rails_are_omitted(tmp_path):
    text = 'goal: "g"\nroles:\n  - name: w\n    subscribes_to: [task.created]\n    prompt: "p"\n'
    cfg = load_run_config(_write(tmp_path, text))
    assert cfg["goal"] == "g"
    assert cfg["roles"][0]["name"] == "w"
    # no rails block -> run_episode's own defaults apply, so keys must be absent
    for k in ("usd_budget", "run_timeout_s", "quiescence_s", "tick_s"):
        assert k not in cfg


def test_null_rails_value_is_omitted(tmp_path):
    # An empty/null YAML value must be omitted, not passed as None, so
    # run_episode's own `.get(key, default)` fallback still applies.
    text = 'goal: "g"\nrails:\n  timeout_s:\n  usd_budget: 2.0\nroles:\n  - name: w\n    subscribes_to: [task.created]\n    prompt: "p"\n'
    cfg = load_run_config(_write(tmp_path, text))
    assert "run_timeout_s" not in cfg  # null timeout_s dropped, default applies
    assert cfg["usd_budget"] == 2.0    # a real value still passes through


def test_missing_goal_raises(tmp_path):
    text = 'roles:\n  - name: w\n    subscribes_to: [task.created]\n    prompt: "p"\n'
    import pytest
    with pytest.raises(KeyError):
        load_run_config(_write(tmp_path, text))
