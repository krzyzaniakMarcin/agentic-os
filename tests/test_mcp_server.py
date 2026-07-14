"""T3 check: MCP syscall boundary — server-side identity, no self-exclusion (arch §5)."""
import inspect
import uuid

import pytest

from substrate import log, mcp_server


def _run_id() -> str:
    return f"test-{uuid.uuid4()}"


@pytest.fixture(autouse=True)
async def _close_pool():
    yield
    await log.close()


@pytest.fixture
def _identity(monkeypatch):
    """Set the per-session identity the harness would set on the subprocess."""
    rid = _run_id()
    monkeypatch.setenv("AGENT_NAME", "worker")
    monkeypatch.setenv("RUN_ID", rid)
    return rid


async def test_emit_event_stamps_identity_from_env(_identity):
    rid = _identity
    r = await mcp_server.emit_event("claim.made", {"answer": 42})
    assert isinstance(r["id"], int) and r["id"] > 0
    assert isinstance(r["ts"], float)
    # Identity was stamped from env, not from any argument.
    rows = await log.read_events(run_id=rid)
    assert len(rows) == 1
    assert rows[0]["agent"] == "worker"
    assert rows[0]["run_id"] == rid
    assert rows[0]["payload"] == {"v": 1, "answer": 42}


async def test_emit_event_has_no_agent_or_run_id_param():
    # The identity params must not exist on the syscall surface at all.
    params = set(inspect.signature(mcp_server.emit_event).parameters)
    assert "agent" not in params
    assert "run_id" not in params
    assert params == {"type", "payload", "reply_to", "correlation"}


async def test_emit_event_requires_identity(monkeypatch):
    monkeypatch.delenv("AGENT_NAME", raising=False)
    monkeypatch.setenv("RUN_ID", _run_id())
    with pytest.raises(RuntimeError):
        await mcp_server.emit_event("claim.made", {})


async def test_read_events_is_run_scoped_and_returns_own_events(_identity):
    # Two emits from the same agent; the MCP read must return BOTH — no
    # self-exclusion on the syscall surface (that's the harness's job, §6).
    await mcp_server.emit_event("claim.made", {"i": 1})
    await mcp_server.emit_event("run.complete", {"i": 2})
    rows = await mcp_server.read_events()
    assert [r["type"] for r in rows] == ["claim.made", "run.complete"]
    assert {r["agent"] for r in rows} == {"worker"}  # caller sees its own events


async def test_read_events_passes_through_filters(_identity):
    await mcp_server.emit_event("claim.made", {})
    await mcp_server.emit_event("claim.rejected", {})
    await mcp_server.emit_event("critique.made", {})
    rows = await mcp_server.read_events(types=["claim.*"])
    assert sorted(r["type"] for r in rows) == ["claim.made", "claim.rejected"]


async def test_read_events_only_sees_its_own_run(monkeypatch):
    # run_id is derived from env, not a client param — a session cannot read
    # another run's events.
    rid_a, rid_b = _run_id(), _run_id()
    monkeypatch.setenv("AGENT_NAME", "worker")
    monkeypatch.setenv("RUN_ID", rid_a)
    await mcp_server.emit_event("claim.made", {"run": "a"})
    monkeypatch.setenv("RUN_ID", rid_b)
    rows = await mcp_server.read_events()
    assert rows == []  # nothing from run a leaks into run b


async def test_read_events_caps_limit(_identity):
    # A client-supplied limit is capped server-side (trust boundary).
    for i in range(6):
        await mcp_server.emit_event("claim.made", {"i": i})
    rows = await mcp_server.read_events(limit=10_000_000)
    assert len(rows) <= 500
    assert len(rows) == 6  # everything under the cap still comes back


async def test_read_events_has_no_run_id_or_exclude_param():
    params = set(inspect.signature(mcp_server.read_events).parameters)
    assert "run_id" not in params
    assert "exclude_agent" not in params
    assert params == {"since_id", "types", "correlation", "limit"}
