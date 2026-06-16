"""Item 1 — the pluggable sandbox driver.

The srt driver must enforce a network egress allowlist on the whole process
tree (Boundary runs bash, which spawns curl). Egress is tested against a
loopback sink so the assertion is deterministic and needs no real network.
"""
from __future__ import annotations

import os
import platform
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from boundary.tools.sandbox import run_sandboxed

SRT_AVAILABLE = shutil.which("srt") is not None
SEATBELT_AVAILABLE = platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None


def _loopback_sink():
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.mark.skipif(not SRT_AVAILABLE, reason="srt required")
def test_srt_blocks_child_process_egress_under_empty_allowlist(tmp_path):
    srv, port = _loopback_sink()
    try:
        out = run_sandboxed(
            f"curl -sS -m 5 http://127.0.0.1:{port} -o /dev/null && echo NET_OK || echo NET_BLOCKED",
            workspace_root=tmp_path, timeout=30, driver="srt", egress_allowlist=[],
        )
        assert "NET_OK" not in out, f"egress was NOT blocked: {out!r}"
    finally:
        srv.shutdown()


def test_agent_threads_driver_to_bash_tool(tmp_path):
    # The Agent must route its sandbox_driver down to the bash tool.
    if os.name != "posix":
        pytest.skip("bash tool requires a POSIX shell — Windows backend not implemented")
    from boundary.agent import Agent

    agent = Agent(
        name="t", system_prompt="x", workspace=tmp_path, client=object(),
        enable_fs=False, enable_shell=True, enable_web=False,
        sandbox_driver="none", transcript=False,
    )
    out = agent.tools.get("bash").call({"command": "echo hi", "reason": "test"})
    assert "UNSANDBOXED" in out and "hi" in out


@pytest.mark.skipif(not SEATBELT_AVAILABLE, reason="macOS sandbox-exec required")
def test_seatbelt_driver_jails_writes_to_workspace(tmp_path):
    # A write outside the workspace must fail under the seatbelt driver.
    out = run_sandboxed(
        "echo pwned > ../escape.txt && echo WROTE || echo DENIED",
        workspace_root=tmp_path, timeout=30, driver="seatbelt", egress_allowlist=None,
    )
    assert not (tmp_path.parent / "escape.txt").exists()
