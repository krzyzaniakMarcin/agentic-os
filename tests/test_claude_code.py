"""T5 check: Claude Code runtime — prompt build, usage parse, step contract,
rate-limit wait-and-resume. No live `claude -p`; the runner is injected."""
import pytest

from agent.role import Role
from agent.runtimes.claude_code import (
    ClaudeCodeAgent,
    _build_prompt,
    _parse_usage,
)


def _role(prompt="You are a solver.", name="worker"):
    return Role(name=name, subscribes_to=["task.created"], prompt=prompt)


def test_build_prompt_includes_role_and_events():
    events = [{"id": 3, "agent": "kernel", "type": "task.created", "payload": {"goal": "answer 6*7"}}]
    p = _build_prompt("You are a solver.", events)
    assert "You are a solver." in p
    assert "task.created" in p
    assert "answer 6*7" in p
    assert "substrate" in p.lower()  # tells the model how to respond


def test_parse_usage_passthrough():
    result = {"usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.001}
    assert _parse_usage(result) == {"input_tokens": 10, "output_tokens": 5, "total_cost_usd": 0.001}


def test_parse_usage_empty():
    assert _parse_usage({}) == {}


async def test_step_returns_no_emits_and_usage():
    captured = {}

    async def fake_runner(prompt, env):
        captured["prompt"] = prompt
        captured["env"] = env
        return {"usage": {"input_tokens": 3}, "total_cost_usd": 0.0}

    a = ClaudeCodeAgent(_role(), run_id="r1", runner=fake_runner)
    events = [{"id": 1, "agent": "kernel", "type": "task.created", "payload": {"goal": "x"}}]
    emits, usage = await a.step(events)

    assert emits == []  # the model emits via the MCP, not the harness
    assert usage == {"input_tokens": 3, "total_cost_usd": 0.0}
    assert "task.created" in captured["prompt"]
    # identity is stamped on the subprocess env for the MCP server (T3)
    assert captured["env"]["AGENT_NAME"] == "worker"
    assert captured["env"]["RUN_ID"] == "r1"
