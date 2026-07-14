"""Agent-facing MCP server — the syscall boundary (arch §5).

Exposes exactly `emit_event` + `read_events` to every agent as an MCP server
named `substrate`. Stdio transport: each stateless `claude -p` step (T5)
spawns a fresh server process, so the server learns its identity from env
vars set on that subprocess — `AGENT_NAME` (the emitter, stamped server-side,
never trusted from the client) and `RUN_ID` (the session scope). Monotonic
visibility across these many short-lived writer connections is carried by the
advisory lock in log.py (T2), not by any shared server.

Self-exclusion is NOT here — that's the harness drive loop's job (§6). This
`read_events` returns everything, including the caller's own events, because
agents legitimately query their own history ("what did I already claim?").
"""
import os

from mcp.server.fastmcp import FastMCP

from substrate import log

mcp = FastMCP("substrate")


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:  # fail fast: a server with no identity must never stamp None
        raise RuntimeError(f"{name} not set — the harness must set it on the MCP subprocess")
    return v


@mcp.tool()
async def emit_event(
    type: str, payload: dict, reply_to: int | None = None, correlation: str | None = None
) -> dict:
    """Publish an event to the shared log. The only way to communicate with other agents."""
    return await log.emit(
        agent=_require("AGENT_NAME"),
        type=type,
        payload=payload,
        run_id=_require("RUN_ID"),
        reply_to=reply_to,
        correlation=correlation,
    )


@mcp.tool()
async def read_events(
    since_id: int = 0,
    types: list[str] | None = None,
    correlation: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read events from the shared log, filtered. Use to observe other agents.

    Implicitly scoped to this session's run_id (server-derived). Returns the
    caller's own events too — self-exclusion lives only in the harness (§6).
    """
    return await log.read_events(
        run_id=_require("RUN_ID"),
        since_id=since_id,
        types=types,
        correlation=correlation,
        limit=min(max(limit, 0), 500),  # cap client-supplied limit — trust boundary
    )


if __name__ == "__main__":  # stdio transport: one process per `claude -p` step (T5)
    mcp.run()
