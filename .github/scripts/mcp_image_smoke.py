"""The engine image smoke's MCP check: one real tool call through the served ``python -m`` app.

Fed to the container's python by CI (``docker exec -i smoke python - < this file``), this
runs inside the engine container against the app uvicorn actually serves — the layout in
which ``sys.modules["__main__"].app`` is the lifespan-bound app, and importing
``hydra.dashboard.api`` would build a second, stateless instance. That second instance is
the 2.5.0 production failure: every DB/session MCP tool answered its 503 envelope while
initialize and tools/list worked, and no pipeline coverage drove a tool call through the
real layout. One ``session_list`` call that is not an error and parses to a list (empty is
fine — no sessions run in the smoke) proves the tools resolve the served engine's state.
"""

from __future__ import annotations

import json
import os
import sys
from http.client import HTTPConnection, HTTPResponse
from typing import Any

ACCEPT = "application/json, text/event-stream"


def fail(message: str) -> None:
    print(f"::error::mcp image smoke: {message}", file=sys.stderr)
    sys.exit(1)


def post(
    payload: dict[str, Any], token: str, session_id: str | None = None
) -> tuple[HTTPResponse, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": ACCEPT,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    conn = HTTPConnection("localhost", 8000, timeout=30)
    conn.request("POST", "/mcp/", json.dumps(payload), headers)
    response = conn.getresponse()
    body = response.read().decode()
    conn.close()
    return response, body


def rpc(response: HTTPResponse, body: str) -> Any:
    """The JSON-RPC message: SSE-framed (the SDK's default) or plain JSON."""
    if (response.getheader("content-type") or "").startswith("text/event-stream"):
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data:
                return json.loads(data)
        fail(f"no data frame in SSE body: {body!r}")
    return json.loads(body)


def main() -> None:
    token = os.environ.get("HYDRA_MCP_TOKEN")
    if not token:
        fail("HYDRA_MCP_TOKEN is not set in the container")
    response, body = post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "ci-image-smoke", "version": "0.0.0"},
            },
        },
        token,
    )
    if response.status != 200:
        fail(f"initialize returned HTTP {response.status}: {body!r}")
    session_id = response.getheader("mcp-session-id")
    if not session_id:
        fail("initialize returned no Mcp-Session-Id header")
    server = rpc(response, body).get("result", {}).get("serverInfo", {})
    if server.get("name") != "hydra":
        fail(f"initialize serverInfo is not hydra: {server!r}")

    response, body = post(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, token, session_id
    )
    if response.status != 202:
        fail(f"notifications/initialized returned HTTP {response.status}: {body!r}")

    response, body = post(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "session_list", "arguments": {}},
        },
        token,
        session_id,
    )
    if response.status != 200:
        fail(f"tools/call session_list returned HTTP {response.status}: {body!r}")
    result = rpc(response, body).get("result", {})
    if result.get("isError", False):
        fail(f"tools/call session_list is an error: {result!r}")
    content = result.get("content") or []
    if len(content) != 1 or content[0].get("type") != "text":
        fail(f"unexpected session_list content: {result!r}")
    sessions = json.loads(content[0]["text"])
    if not isinstance(sessions, list):
        fail(f"session_list did not return a list: {sessions!r}")
    print(f"mcp session_list through the served app: {len(sessions)} sessions")


if __name__ == "__main__":
    main()
