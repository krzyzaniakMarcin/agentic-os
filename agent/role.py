"""Agent role config (arch §6, §7): subscriptions + prompt drive one poll loop."""
from dataclasses import dataclass, field, fields


@dataclass
class Role:
    name: str
    subscribes_to: list[str]
    emits: list[str] = field(default_factory=list)
    prompt: str = ""
    see_own_events: bool = False  # deliver the agent's own emissions back to it? (arch §6)
    tick_s: float = 0.5  # idle poll interval


def load_role(data: dict) -> Role:
    """Build a Role from a config dict, dropping keys Role doesn't model."""
    known = {f.name for f in fields(Role)}
    return Role(**{k: v for k, v in data.items() if k in known})
