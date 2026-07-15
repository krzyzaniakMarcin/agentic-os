"""T7 check: run_phase0 orchestration — seed, drive one agent, wait for
run.complete, dump. Uses an in-memory log double + a fake claude runner that
simulates the model emitting through the MCP."""
from agent.runtimes import claude_code
from substrate import log


def _install_memory_log(monkeypatch):
    """List-backed log double matching the subset of log.* run_phase0 uses."""
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


async def test_run_phase0_completes_and_records_answer(monkeypatch):
    from scripts import run_phase0

    store = _install_memory_log(monkeypatch)

    async def fake_runner(prompt, env):
        # Simulate the model emitting through the substrate MCP during the step.
        await log.emit(env["AGENT_NAME"], "claim.made", {"answer": "Paris"},
                       run_id=env["RUN_ID"])
        await log.emit(env["AGENT_NAME"], "run.complete", {}, run_id=env["RUN_ID"])
        return {"usage": {"input_tokens": 1}}

    monkeypatch.setattr(claude_code, "_run_claude", fake_runner)

    await run_phase0.main()

    types = [e["type"] for e in store]
    assert "run.start" in types
    assert "task.created" in types
    assert "claim.made" in types
    assert "run.complete" in types
    assert "agent.step" in types  # the harness recorded the step
