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


import time

from agent.runtimes.claude_code import _rate_limit_wait_s


def test_rate_limit_wait_s_not_error_returns_none():
    assert _rate_limit_wait_s({"usage": {}}, now=1000.0) is None


def test_rate_limit_wait_s_reset_at_epoch():
    r = {"is_error": True, "error": {"type": "rate_limit_error", "reset_at": 1060.0}}
    assert _rate_limit_wait_s(r, now=1000.0) == 60.0


def test_rate_limit_wait_s_retry_after_delta():
    r = {"is_error": True, "error": {"type": "rate_limit_error", "retry_after": 30.0}}
    assert _rate_limit_wait_s(r, now=1000.0) == 30.0


def test_rate_limit_wait_s_missing_reset_uses_backoff():
    from agent.runtimes.claude_code import _DEFAULT_BACKOFF_S
    r = {"is_error": True, "error": {"type": "rate_limit_error"}}
    assert _rate_limit_wait_s(r, now=1000.0) == _DEFAULT_BACKOFF_S


def test_rate_limit_wait_s_capped():
    from agent.runtimes.claude_code import _MAX_WAIT_S
    r = {"is_error": True, "error": {"type": "rate_limit_error", "reset_at": 10**12}}
    assert _rate_limit_wait_s(r, now=0.0) == _MAX_WAIT_S


def test_non_rate_limit_error_is_not_retried():
    r = {"is_error": True, "error": {"type": "some_other_error"}}
    assert _rate_limit_wait_s(r, now=1000.0) is None


async def test_step_retries_on_rate_limit_then_succeeds(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("agent.runtimes.claude_code.asyncio.sleep", fake_sleep)

    calls = []

    async def flaky_runner(prompt, env):
        calls.append(1)
        if len(calls) == 1:
            return {"is_error": True, "error": {"type": "rate_limit_error", "retry_after": 5.0}}
        return {"usage": {"input_tokens": 2}}

    a = ClaudeCodeAgent(_role(), run_id="r1", runner=flaky_runner)
    emits, usage = await a.step([{"id": 1, "type": "task.created", "payload": {}}])

    assert len(calls) == 2  # retried once
    assert slept == [5.0]  # waited the reset window
    assert usage == {"input_tokens": 2}


async def test_step_gives_up_after_cap(monkeypatch):
    from agent.runtimes.claude_code import _MAX_RETRIES

    async def fake_sleep(s):
        pass

    monkeypatch.setattr("agent.runtimes.claude_code.asyncio.sleep", fake_sleep)

    calls = []

    async def always_limited(prompt, env):
        calls.append(1)
        return {"is_error": True, "error": {"type": "rate_limit_error", "retry_after": 1.0}}

    a = ClaudeCodeAgent(_role(), run_id="r1", runner=always_limited)
    emits, usage = await a.step([{"id": 1, "type": "task.created", "payload": {}}])

    assert len(calls) == _MAX_RETRIES + 1  # initial try + capped retries
    assert emits == []  # still returns cleanly (usage from the limit result)


def test_rate_limit_wait_s_non_numeric_reset_uses_backoff():
    from agent.runtimes.claude_code import _DEFAULT_BACKOFF_S
    r = {"is_error": True, "error": {"type": "rate_limit_error", "reset_at": "2026-07-15T00:00:00Z"}}
    assert _rate_limit_wait_s(r, now=1000.0) == _DEFAULT_BACKOFF_S


def test_subprocess_env_stamps_identity(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    a = ClaudeCodeAgent(_role(name="solver"), run_id="run-9")
    env = a._subprocess_env()
    assert env["AGENT_NAME"] == "solver"
    assert env["RUN_ID"] == "run-9"


def test_subprocess_env_enables_telemetry_when_otlp_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://langfuse-web:3000/api/public/otel")
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_TELEMETRY", raising=False)
    a = ClaudeCodeAgent(_role(), run_id="r1")
    env = a._subprocess_env()
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"


def test_subprocess_env_no_telemetry_without_otlp(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_TELEMETRY", raising=False)
    a = ClaudeCodeAgent(_role(), run_id="r1")
    env = a._subprocess_env()
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in env
