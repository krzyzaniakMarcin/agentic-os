"""Claude Code runtime (T5): stateless per-step `claude -p` subprocess.

The log is the memory (arch §6): each step renders new_events into a prompt,
runs one `claude -p` invocation, and returns usage only. The MODEL emits
claim.made / run.complete through the substrate MCP (identity stamped
server-side from AGENT_NAME/RUN_ID, T3), so step() returns emits=[] (T4).

The subprocess call is injectable (`runner`) so tests use a fake. The real
`claude -p` path (`_run_claude`) is FIRST EXERCISED LIVE by the orchestrator
(kernel/orchestrator.py, which absorbed T7's run_phase0) — it is unproven until
then. Do not drop that live run: it validates the CLI + MCP wiring this module
only stubs in tests.
"""
from __future__ import annotations

import asyncio
import json
import os
from functools import partial
from pathlib import Path

from agent.base import Agent
from agent.role import Role

# repo-root-relative so it works regardless of CWD (agent/runtimes/ → ../../).
_MCP_CONFIG = Path(__file__).resolve().parents[2] / "config" / "claude" / ".mcp.json"

# ponytail: bounded single-agent rate-limit wait-and-resume. Fixed retry cap +
# max wait is enough for one coroutine — while it sleeps it costs zero tokens
# and other agents keep ticking (asyncio). The COORDINATED fleet-wide version
# (pause the whole fleet on a shared limit) is a Phase 1 rail (arch §9);
# upgrade path: replace this per-agent sleep with a shared limit rail.
_MAX_RETRIES = 5
_MAX_WAIT_S = 3600.0
_DEFAULT_BACKOFF_S = 60.0


class ClaudeCodeAgent(Agent):
    """Stateless per-step `claude -p`. Returns usage only; emits=[] (arch §6)."""

    def __init__(self, role: Role, run_id: str, runner=None):
        super().__init__(role, run_id)
        self.prompt = role.prompt
        self._runner = runner or partial(_run_claude, max_budget_usd=role.max_budget_usd)

    async def step(self, new_events: list[dict]) -> tuple[list, dict]:
        prompt = _build_prompt(self.prompt, new_events)
        env = self._subprocess_env()
        result = await self._invoke_with_retry(prompt, env)
        return [], _parse_usage(result)

    def _subprocess_env(self) -> dict:
        env = dict(os.environ)  # inherit ANTHROPIC_API_KEY + any OTEL_* set by T6
        env["AGENT_NAME"] = self.name  # MCP server stamps the emitter from this (T3)
        env["RUN_ID"] = self.run_id
        # Claude Code's own OTel export → Langfuse OTLP gives per-model/per-tool
        # spans INSIDE the subprocess (T6 owns the endpoint/creds config in the
        # environment; T5 only turns telemetry on when an endpoint is present —
        # without this you get one span per step, not per call).
        if env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            env.setdefault("CLAUDE_CODE_ENABLE_TELEMETRY", "1")
        return env

    async def _invoke_with_retry(self, prompt: str, env: dict) -> dict:
        import time

        for attempt in range(_MAX_RETRIES + 1):
            result = await self._runner(prompt, env)
            wait = _rate_limit_wait_s(result, time.time())
            if wait is None or attempt == _MAX_RETRIES:
                # ponytail: non-limit errors (auth, invalid request, bad model
                # turn) return here and the loop advances the cursor past the
                # triggering events with no run.complete emitted — a silent
                # drop, acceptable for the P0 demo. Surface them via the log
                # (an error event / harness signal) if agents must recover.
                return result  # success, non-limit error, or out of retries
            await asyncio.sleep(wait)  # idle at zero token cost; cursor lives in the log


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


def _rate_limit_wait_s(result: dict, now: float) -> float | None:
    """Seconds to wait before re-running the step, or None if not rate-limited.

    ponytail: the exact shape of a `claude -p` rate-limit result is verified
    live in T7. This tolerantly checks the likely fields (rate_limit/overloaded
    error type; reset_at epoch or retry_after delta) and falls back to a fixed
    backoff. Tighten the field names once T7 shows the real schema."""
    if not result.get("is_error"):
        return None
    err = result.get("error") or {}
    etype = err.get("type") or result.get("subtype") or ""
    if "rate_limit" not in etype and "overloaded" not in etype:
        return None
    try:
        if "retry_after" in err:
            wait = float(err["retry_after"])
        elif "reset_at" in err:
            wait = float(err["reset_at"]) - now
        else:
            wait = _DEFAULT_BACKOFF_S
    except (TypeError, ValueError):
        wait = _DEFAULT_BACKOFF_S  # ponytail: schema unverified until T7 — degrade, don't crash
    return min(max(0.0, wait), _MAX_WAIT_S)


def _build_argv(prompt: str, max_budget_usd: float | None) -> list[str]:
    """The `claude -p` argv. Per-session cost cap (T13) is appended when set.

    ponytail: the installed CLI (2.1.211) has no `--max-turns`; `--max-budget-usd`
    is the per-session runaway rail the T13 spec allows as the cost-cap alternative.
    Swap/extend here if a turn-count flag returns."""
    argv = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--mcp-config", str(_MCP_CONFIG),
        # non-interactive tool use: let the model call only the substrate tools
        "--allowedTools", "mcp__substrate__emit_event,mcp__substrate__read_events",
    ]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", str(max_budget_usd)]
    return argv


async def _run_claude(prompt: str, env: dict, max_budget_usd: float | None = None) -> dict:
    """Real subprocess. FIRST LIVE invocation happens via the orchestrator —
    tests inject a fake runner, so this path is unproven until
    kernel/orchestrator.py runs it live."""
    proc = await asyncio.create_subprocess_exec(
        *_build_argv(prompt, max_budget_usd),
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
