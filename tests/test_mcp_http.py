"""Tests for the gateway's HTTP transport (serve_http + bearer auth).

BearerAuthASGI is pure ASGI and tested without any MCP dependency. The full
transport round-trip (mcp-serve subprocess + streamable-http client) runs only
when the optional `mcp` SDK is installed, mirroring how the extra ships.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from boundary.mcp_gateway import BearerAuthASGI, make_token


# ------------------------------------------------------ auth unit (no mcp)

class _RecordingApp:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(auth: str | None):
    headers = [(b"authorization", auth.encode())] if auth is not None else []
    return {"type": "http", "headers": headers}


def _run_asgi(app, scope):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(app(scope, receive, send))
    return sent


def test_bearer_auth_rejects_missing_and_wrong_token():
    inner = _RecordingApp()
    app = BearerAuthASGI(inner, "secret")
    for auth in (None, "Bearer wrong", "secret", "bearer secret"):
        inner.called = False
        sent = _run_asgi(app, _http_scope(auth))
        assert sent[0]["status"] == 401, auth
        assert inner.called is False, auth


def test_bearer_auth_accepts_exact_token_and_passes_through():
    inner = _RecordingApp()
    app = BearerAuthASGI(inner, "secret")
    sent = _run_asgi(app, _http_scope("Bearer secret"))
    assert inner.called is True
    assert sent[0]["status"] == 200


def test_empty_token_disables_the_gate():
    inner = _RecordingApp()
    app = BearerAuthASGI(inner, "")
    sent = _run_asgi(app, _http_scope(None))
    assert inner.called is True
    assert sent[0]["status"] == 200


def test_non_http_scopes_pass_through_untouched():
    inner = _RecordingApp()
    app = BearerAuthASGI(inner, "secret")
    _run_asgi(app, {"type": "lifespan"})
    assert inner.called is True


def test_make_token_is_long_and_unique():
    a, b = make_token(), make_token()
    assert a != b
    assert len(a) >= 32


# ------------------------------------------- full transport (needs mcp SDK)

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.skipif(
    os.environ.get("BOUNDARY_SKIP_HTTP_E2E") == "1",
    reason="explicitly skipped",
)
def test_http_transport_end_to_end(tmp_path):
    mcp = pytest.importorskip("mcp")  # noqa: F841
    pytest.importorskip("uvicorn")
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import (
        create_mcp_http_client,
        streamable_http_client,
    )

    ws = tmp_path / "ws"
    port = _free_port()
    token = make_token()
    proc = subprocess.Popen(
        [sys.executable, "-m", "boundary.cli", "mcp-serve",
         "--transport", "http", "--port", str(port),
         "--workspace", str(ws),
         "--envelope-writable", "out/**", "--no-staging-gate", "--no-transcript"],
        env=dict(os.environ, BOUNDARY_MCP_TOKEN=token),
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), 0.25).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("gateway HTTP server did not come up")

        assert httpx.post(url, json={}).status_code == 401

        async def drive():
            http = create_mcp_http_client(
                headers={"Authorization": f"Bearer {token}"})
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = [t.name for t in tools.tools]
                    assert "boundary_write_file" in names
                    ok = await session.call_tool("boundary_write_file", {
                        "path": "out/h.txt", "content": "http", "reason": "t"})
                    assert "wrote" in ok.content[0].text
                    refused = await session.call_tool("boundary_write_file", {
                        "path": "nope/h.txt", "content": "x", "reason": "t"})
                    assert refused.content[0].text.startswith("ENVELOPE REFUSED")

        asyncio.run(drive())
        assert (ws / "out" / "h.txt").read_text() == "http"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
