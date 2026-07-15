"""Claude Code runtime (T5): stateless per-step `claude -p` subprocess.

The log is the memory (arch §6): each step renders new_events into a prompt,
runs one `claude -p` invocation, and returns usage only. The MODEL emits
claim.made / run.complete through the substrate MCP (identity stamped
server-side from AGENT_NAME/RUN_ID, T3), so step() returns emits=[] (T4).

The subprocess call is injectable (`runner`) so tests use a fake. The real
`claude -p` path (`_run_claude`) is FIRST EXERCISED LIVE IN T7
(scripts/run_phase0.py) — it is unproven until then. Do not drop the T7 live
run: it validates the CLI + MCP wiring this module only stubs in tests.
"""
import asyncio
import json
import os
from pathlib import Path

from agent.base import Agent
from agent.role import Role

# repo-root-relative so it works regardless of CWD (agent/runtimes/ → ../../).
_MCP_CONFIG = Path(__file__).resolve().parents[2] / "config" / "claude" / ".mcp.json"


class ClaudeCodeAgent(Agent):
    """Stateless per-step `claude -p`. Returns usage only; emits=[] (arch §6)."""

    def __init__(self, role: Role, run_id: str, runner=None):
        super().__init__(role, run_id)
        self.prompt = role.prompt
        self._runner = runner or _run_claude  # injectable; real subprocess by default

    async def step(self, new_events: list[dict]) -> tuple[list, dict]:
        prompt = _build_prompt(self.prompt, new_events)
        env = self._subprocess_env()
        result = await self._runner(prompt, env)
        return [], _parse_usage(result)

    def _subprocess_env(self) -> dict:
        env = dict(os.environ)  # inherit ANTHROPIC_API_KEY + any OTEL_* set by T6
        env["AGENT_NAME"] = self.name  # MCP server stamps the emitter from this (T3)
        env["RUN_ID"] = self.run_id
        return env


def _build_prompt(role_prompt: str, new_events: list[dict]) -> str:
    lines = [role_prompt.strip(), "", "New events since your last step:"]
    for e in new_events:
        lines.append(json.dumps({k: e.get(k) for k in ("id", "agent", "type", "payload")}))
    lines += ["", "React by emitting events with the substrate MCP tools."]
    return "\n".join(lines)


def _parse_usage(result: dict) -> dict:
    usage = dict(result.get("usage") or {})
    if "total_cost_usd" in result:
        usage["total_cost_usd"] = result["total_cost_usd"]
    return usage


async def _run_claude(prompt: str, env: dict) -> dict:
    """Real subprocess. FIRST LIVE invocation happens in T7 — tests inject a
    fake runner, so this path is unproven until scripts/run_phase0.py runs it."""
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        "--output-format", "json",
        "--mcp-config", str(_MCP_CONFIG),
        # non-interactive tool use: let the model call only the substrate tools
        "--allowedTools", "mcp__substrate__emit_event,mcp__substrate__read_events",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate()
    if not out:
        raise RuntimeError(
            f"claude -p produced no output (rc={proc.returncode}): {err.decode()[:500]}"
        )
    return json.loads(out)
