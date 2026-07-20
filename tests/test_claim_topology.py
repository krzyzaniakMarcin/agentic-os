# tests/test_claim_topology.py — T16: two same-role workers, distinct identities.
from pathlib import Path

import yaml

from agent.role import load_role
from kernel.config import load_run_config

_TOPO = Path(__file__).resolve().parents[1] / "topologies" / "supervisor_claim.yaml"


def test_two_workers_with_distinct_names():
    cfg = load_run_config(_TOPO)
    names = [r["name"] for r in cfg["roles"]]
    assert names == ["supervisor", "worker-1", "worker-2"]
    # Distinct names are mandatory: colliding names collide step_n in the
    # agent.step record and corrupt per-agent projections (phase1-plan §4).
    assert len(set(names)) == len(names)


def test_workers_share_one_prompt_and_subscription():
    cfg = load_run_config(_TOPO)
    roles = {r["name"]: r for r in cfg["roles"]}
    w1, w2 = roles["worker-1"], roles["worker-2"]
    assert w1["prompt"] == w2["prompt"]          # shared via YAML anchor
    assert w1["subscribes_to"] == w2["subscribes_to"] == ["task.assigned"]
    assert roles["supervisor"]["subscribes_to"] == ["task.created", "claim.made"]


def test_worker_prompt_is_anchored_not_duplicated():
    # The YAML source must use an anchor/alias, so a prompt edit can never
    # drift the two workers apart into different claim logic.
    src = _TOPO.read_text()
    assert "&claim_prompt" in src and "*claim_prompt" in src


def test_worker_prompt_encodes_the_claim_protocol():
    # Model-driven claim logic: if these words go, the protocol goes with them.
    cfg = load_run_config(_TOPO)
    worker = {r["name"]: r["prompt"] for r in cfg["roles"]}["worker-1"]
    assert "task.claimed" in worker      # the claim marker
    assert "read_events" in worker       # the single ad-hoc read
    assert "correlation" in worker       # how the read is filtered
    assert "lowest" in worker.lower()    # lowest-id-wins
    assert "claim.made" in worker        # the winner's output
    assert "nothing" in worker.lower()   # the loser emits nothing


def test_roles_load_and_carry_prompts():
    cfg = load_run_config(_TOPO)
    for r in cfg["roles"]:
        role = load_role(r)          # must not raise; unknown keys dropped
        assert role.prompt.strip()


def test_topology_carries_rails():
    cfg = load_run_config(_TOPO)
    assert cfg["usd_budget"] is not None
    assert cfg["run_timeout_s"] > 0
    assert cfg["quiescence_s"] > 0


def test_no_agent_shares_a_name_with_another():
    # Guards the whole roles list, not just the workers.
    raw = yaml.safe_load(_TOPO.read_text())
    names = [r["name"] for r in raw["roles"]]
    assert len(set(names)) == len(names)
