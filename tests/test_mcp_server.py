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
