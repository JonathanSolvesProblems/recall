"""Schema introspection through the CockroachDB Cloud Managed MCP Server.

Cockroach Labs' stated reason for shipping this is schema hallucination: an
agent that writes SQL from a remembered schema produces "brittle queries,
schema mismatches, or unnecessary load". So the agent asks the cluster what
the schema currently is instead of assuming.

Read-only by design. The key this uses carries the `mcp:read` scope, so the
introspection path physically cannot write, independent of what any prompt
talks the model into.

The endpoint speaks JSON-RPC 2.0 over Streamable HTTP. SSE is deliberately
excluded by the server (deprecated in MCP), so responses arrive either as
plain JSON or as an `event:`/`data:` framed stream, and both are handled.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from unsay.config import settings

log = logging.getLogger(__name__)


class MCPUnavailable(RuntimeError):
    """Raised when the MCP server cannot be reached or is not configured.

    Deliberately not swallowed. A caller that silently falls back to a
    hardcoded schema when introspection fails has reintroduced exactly the
    hallucination this module exists to prevent, and would do it invisibly.
    """


def _headers() -> dict[str, str]:
    cfg = settings()
    if not cfg.crdb_mcp_api_key:
        raise MCPUnavailable("CRDB_MCP_API_KEY is not set")
    if not cfg.crdb_cluster_id:
        raise MCPUnavailable("CRDB_CLUSTER_ID is not set")
    return {
        "Authorization": f"Bearer {cfg.crdb_mcp_api_key}",
        "mcp-cluster-id": cfg.crdb_cluster_id,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _parse(resp: httpx.Response) -> dict[str, Any]:
    """Read a JSON-RPC reply that may arrive plain or event-stream framed."""
    body = resp.text.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise MCPUnavailable(f"unparseable MCP response: {body[:200]}")


def _rpc(method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
    cfg = settings()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(cfg.crdb_mcp_endpoint, headers=_headers(), json=payload)
    except httpx.HTTPError as exc:
        raise MCPUnavailable(f"MCP transport error: {exc}") from exc

    if resp.status_code >= 400:
        raise MCPUnavailable(f"MCP HTTP {resp.status_code}: {resp.text[:200]}")

    data = _parse(resp)
    if "error" in data:
        raise MCPUnavailable(f"MCP error: {data['error']}")
    return data.get("result")


def list_tools() -> list[dict[str, Any]]:
    """Tools the server exposes. Useful as a connectivity check."""
    return (_rpc("tools/list") or {}).get("tools", [])


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    return _rpc("tools/call", {"name": name, "arguments": arguments or {}})


def describe_table(table: str, database: str = "unsay") -> str:
    """Current column definitions for one table, straight from the cluster."""
    result = call_tool("get_table_schema", {"database": database, "table": table})
    content = (result or {}).get("content", [])
    return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))


def live_schema(tables: tuple[str, ...] = ("fact", "decision", "decision_read")) -> str:
    """The schema block handed to the model before it reasons about the data.

    Only the tables that matter to a memory question. Sending the whole
    catalogue would spend context on tables the model has no business
    querying, and a larger prompt is not a better-grounded one.
    """
    parts = []
    for t in tables:
        try:
            parts.append(f"-- {t}\n{describe_table(t)}")
        except MCPUnavailable as exc:
            log.warning("MCP introspection failed for %s: %s", t, exc)
            raise
    return "\n\n".join(parts)
