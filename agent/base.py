"""Runtime contract (arch §6): the poll loop drives step(); step turns new
events into (emits, usage). Subclasses (e.g. the Claude Code runtime, T5)
implement step(); the CC runtime returns emits=[] because the model emits
through the substrate MCP directly."""
from dataclasses import dataclass

from agent.role import Role


@dataclass
class Emit:
    type: str
    payload: dict
    reply_to: int | None = None
    correlation: str | None = None


class Agent:
    def __init__(self, role: Role, run_id: str):
        self.name = role.name
        self.run_id = run_id
        self.subscribes_to = role.subscribes_to
        self.see_own_events = role.see_own_events
        self.tick_s = role.tick_s
        self.step_n = 0
        self.in_step = False  # True while step() runs; T11 quiescence reads this
        self._stopped = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        self._stopped = True

    async def step(self, new_events: list[dict]) -> tuple[list[Emit], dict]:
        raise NotImplementedError
