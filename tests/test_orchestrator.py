"""T10 check: orchestrator run_episode — seed, spawn loops, supervise rails,
dump. Uses an in-memory log double + a fake claude runner (no model)."""
from agent.runtimes import claude_code
from agent.runtimes.claude_code import ClaudeCodeAgent
from kernel import orchestrator
from substrate import log


def _install_memory_log(monkeypatch):
    """List-backed log double matching the subset of log.* the kernel uses."""
    store: list[dict] = []

    async def fake_emit(agent, type, payload, run_id, reply_to=None, correlation=None):
        store.append({"id": len(store) + 1, "agent": agent, "type": type,
                      "payload": payload, "run_id": run_id})
        return {"id": len(store), "ts": 0.0}

    async def fake_read(run_id, since_id=0, types=None, correlation=None,
                        limit=50, exclude_agent=None):
        out = [e for e in store
               if e["run_id"] == run_id and e["id"] > since_id
               and (types is None or e["type"] in types)
               and (exclude_agent is None or e["agent"] != exclude_agent)]
        return out[:limit]

    async def fake_close():
        pass

    monkeypatch.setattr(log, "emit", fake_emit)
    monkeypatch.setattr(log, "read_events", fake_read)
    monkeypatch.setattr(log, "close", fake_close)
    return store


async def test_run_complete_terminates_and_records(monkeypatch):
    store = _install_memory_log(monkeypatch)

    async def fake_runner(prompt, env, max_budget_usd=None):
        # Simulate the model emitting through the substrate MCP during the step.
        await log.emit(env["AGENT_NAME"], "claim.made", {"answer": "Paris"},
                       run_id=env["RUN_ID"])
        await log.emit(env["AGENT_NAME"], "run.complete", {}, run_id=env["RUN_ID"])
        return {"usage": {"input_tokens": 1}}

    monkeypatch.setattr(claude_code, "_run_claude", fake_runner)

    cfg = {"goal": "What is the capital of France?",
           "roles": [{"name": "worker", "subscribes_to": ["task.created"],
                      "prompt": "answer it"}]}
    events = await orchestrator.run_episode(cfg)

    types = [e["type"] for e in events]
    for expected in ("run.start", "task.created", "claim.made",
                     "run.complete", "agent.step"):
        assert expected in types


async def test_budget_breach_halts_and_stops_all(monkeypatch):
    _install_memory_log(monkeypatch)

    captured: list[ClaudeCodeAgent] = []

    class Spy(ClaudeCodeAgent):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.append(self)

    monkeypatch.setattr(orchestrator, "ClaudeCodeAgent", Spy)

    async def fake_runner(prompt, env, max_budget_usd=None):  # emits nothing → no run.complete
        return {"usage": {"input_tokens": 1}}

    monkeypatch.setattr(claude_code, "_run_claude", fake_runner)

    class FakeBudget:
        async def breached(self):
            return "usd_budget"

    cfg = {"goal": "g",
           "roles": [{"name": "w1", "subscribes_to": ["task.created"], "prompt": "p"},
                     {"name": "w2", "subscribes_to": ["task.created"], "prompt": "p"}],
           "budget": FakeBudget(), "tick_s": 0.0}
    events = await orchestrator.run_episode(cfg)

    halt = [e for e in events if e["type"] == "system.halt"]
    assert halt and halt[0]["payload"]["reason"] == "usd_budget"
    assert captured and all(a.stopped for a in captured)


async def test_wall_clock_timeout_halts(monkeypatch):
    # No budget in cfg → the wall-clock rail is the sole termination guard for
    # a run whose model never emits run.complete (the docker demo path).
    _install_memory_log(monkeypatch)

    async def fake_runner(prompt, env, max_budget_usd=None):  # never emits run.complete
        return {"usage": {}}

    monkeypatch.setattr(claude_code, "_run_claude", fake_runner)

    cfg = {"goal": "g",
           "roles": [{"name": "w", "subscribes_to": ["task.created"], "prompt": "p"}],
           "run_timeout_s": 0.0, "tick_s": 0.0}
    events = await orchestrator.run_episode(cfg)

    halt = [e for e in events if e["type"] == "system.halt"]
    assert halt and halt[0]["payload"]["reason"] == "timeout"


async def test_quiescence_terminates_without_halt(monkeypatch):
    _install_memory_log(monkeypatch)

    async def fake_runner(prompt, env, max_budget_usd=None):  # emits nothing beyond the loop's agent.step
        return {"usage": {}}

    monkeypatch.setattr(claude_code, "_run_claude", fake_runner)

    cfg = {"goal": "g",
           "roles": [{"name": "w", "subscribes_to": ["task.created"], "prompt": "p"}],
           "quiescence_s": 0.0, "tick_s": 0.0}
    events = await orchestrator.run_episode(cfg)

    # terminated cleanly on quiescence — no budget/timeout halt was needed
    assert not [e for e in events if e["type"] == "system.halt"]
    assert any(e["type"] == "agent.step" for e in events)  # it did do work first
