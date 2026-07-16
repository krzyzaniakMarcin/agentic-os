"""Run-config loader (phase1-plan §T14): YAML topology -> the cfg dict
run_episode consumes (kernel/orchestrator.py). Roles stay raw dicts for
agent.role.load_role (which drops unknown keys), so this is a thin mapper,
not a schema — the substrate acceptance test (§7) requires that expressing a
topology touches only YAML + prompts + this loader.
"""
from __future__ import annotations

from pathlib import Path

import yaml

# YAML rails.<key> -> cfg key run_episode reads. Only timeout_s is renamed
# (run_episode calls it run_timeout_s); the rest pass through unchanged.
_RAILS_KEYS = {
    "usd_budget": "usd_budget",
    "timeout_s": "run_timeout_s",
    "quiescence_s": "quiescence_s",
    "tick_s": "tick_s",
}


def load_run_config(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    cfg: dict = {
        "goal": data["goal"],          # KeyError if absent — a run needs a seed task
        "roles": data["roles"],        # raw dicts; load_role drops unknown keys
    }
    for yaml_key, cfg_key in _RAILS_KEYS.items():
        if yaml_key in (data.get("rails") or {}):
            cfg[cfg_key] = data["rails"][yaml_key]
    return cfg
