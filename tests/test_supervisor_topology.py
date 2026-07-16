# tests/test_supervisor_topology.py
from pathlib import Path

from agent.role import load_role
from kernel.config import load_run_config

_TOPO = Path(__file__).resolve().parents[1] / "topologies" / "supervisor.yaml"


def test_topology_has_supervisor_and_worker():
    cfg = load_run_config(_TOPO)
    roles = {r["name"]: r for r in cfg["roles"]}
    assert set(roles) == {"supervisor", "worker"}
    assert roles["supervisor"]["subscribes_to"] == ["task.created", "claim.made"]
    assert roles["worker"]["subscribes_to"] == ["task.assigned"]


def test_roles_load_and_carry_prompts():
    cfg = load_run_config(_TOPO)
    for r in cfg["roles"]:
        role = load_role(r)          # must not raise; unknown keys dropped
        assert role.prompt.strip()   # every role has a non-empty prompt


def test_prompts_encode_the_correlation_protocol():
    # The stateless-aggregation contract must be spelled out in the prompts,
    # or the live run never terminates (phase1-plan §T14). Assert the key
    # protocol words are present so a prompt edit that drops them is caught.
    cfg = load_run_config(_TOPO)
    roles = {r["name"]: r["prompt"] for r in cfg["roles"]}
    sup, worker = roles["supervisor"], roles["worker"]
    assert "task.assigned" in sup and "claim.made" in sup
    assert "read_events" in sup and "correlation" in sup
    assert "run.complete" in sup
    assert "task.assigned" in worker and "claim.made" in worker
    assert "correlation" in worker


def test_topology_carries_rails():
    cfg = load_run_config(_TOPO)
    assert cfg["usd_budget"] is not None
    assert cfg["run_timeout_s"] > 0
    assert cfg["quiescence_s"] > 0
