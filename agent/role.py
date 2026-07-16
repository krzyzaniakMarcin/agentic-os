"""Agent role config (arch §6, §7): subscriptions + prompt drive one poll loop."""
from __future__ import annotations

from dataclasses import dataclass, field, fields


@dataclass
class Role:
    name: str
    subscribes_to: list[str]
    emits: list[str] = field(default_factory=list)
    prompt: str = ""
    see_own_events: bool = False  # deliver the agent's own emissions back to it? (arch §6)
    tick_s: float = 0.5  # idle poll interval
    # ponytail: per-session runaway cap (T13). Guards ONE `claude -p` turn so a
    # subagent fanout can't blow the global usd_budget (T12) before the kernel
    # reacts between steps. Keep this <= the run's usd_budget or the guard sits
    # above the global and never trips first. Default is generous headroom for
    # the P0 single step; tune per-role via config as real turn costs are known.
    max_budget_usd: float | None = 1.0


def load_role(data: dict) -> Role:
    """Build a Role from a config dict, dropping keys Role doesn't model."""
    known = {f.name for f in fields(Role)}
    return Role(**{k: v for k, v in data.items() if k in known})
