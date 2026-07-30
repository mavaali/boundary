"""Unit tests for the jailed-caller launcher (boundary/launcher.py).

Covers the pure pieces (srt settings shape, placeholder substitution, mcp.json
emission, gateway argv forwarding, caller env) and the fail-closed refusal when
srt is absent. The full jailed path needs srt installed and is exercised by the
GUIDE.md validation steps, not unit tests.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from boundary import launcher
from boundary.launcher import (
    LOOPBACK_DOMAINS,
    caller_env,
    caller_srt_settings,
    gateway_argv,
    launch,
    substitute,
    write_mcp_config,
)


def _args(**over):
    base = dict(
        workspace=".", envelope_writable=["out/**"], envelope_max_writes=5,
        envelope_min_writes=1, envelope_max_appends=10, envelope_max_external=20,
        envelope_max_unstaged_reads=3, no_staging_gate=True, on_taint="warn",
        on_commit="refuse", commit_allow=[], shell=False,
        egress_allow=["api.anthropic.com"], deny_read=[], port=0,
        allow_uncontained=False, keep_env=[],
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_caller_settings_invert_the_bash_jail(tmp_path):
    ws, scratch = tmp_path / "ws", tmp_path / "scratch"
    s = caller_srt_settings(ws, scratch, ["api.anthropic.com"], ["/opt/private"])
    # workspace: readable but NOT writable — mutations must go through the gateway
    assert str(ws) in s["filesystem"]["denyWrite"]
    assert s["filesystem"]["allowWrite"] == [str(scratch)]
    # No allowRead: allowRead takes precedence over denyRead in srt, so setting
    # it to ["/"] would silently defeat the secret denylist (#55 / F26).
    assert "allowRead" not in s["filesystem"]
    # secrets hidden: built-ins plus the caller's extras
    assert any(p.endswith(".ssh") for p in s["filesystem"]["denyRead"])
    assert "/opt/private" in s["filesystem"]["denyRead"]
    # egress: the model API plus loopback for the gateway, nothing else
    assert s["network"]["allowedDomains"] == ["api.anthropic.com"] + LOOPBACK_DOMAINS


def test_substitute_placeholders():
    argv = ["claude", "-p", "task", "--mcp-config", "{MCP_CONFIG}",
            "--note", "{MCP_URL}#{MCP_TOKEN}", "plain"]
    out = substitute(argv, {"MCP_CONFIG": "/tmp/m.json", "MCP_URL": "http://x",
                            "MCP_TOKEN": "tok", "WORKSPACE": "/ws"})
    assert out[4] == "/tmp/m.json"
    assert out[6] == "http://x#tok"
    assert out[7] == "plain"


def test_write_mcp_config(tmp_path):
    p = write_mcp_config(tmp_path / "mcp.json", "http://127.0.0.1:9/mcp", "tok")
    cfg = json.loads(p.read_text())
    server = cfg["mcpServers"]["boundary"]
    assert server["type"] == "http"
    assert server["url"] == "http://127.0.0.1:9/mcp"
    assert server["headers"]["Authorization"] == "Bearer tok"


def test_gateway_argv_forwards_envelope(tmp_path):
    argv = gateway_argv(_args(envelope_writable=["a/**", "b/**"], shell=True), 8123)
    joined = " ".join(argv)
    assert "--transport http" in joined
    assert "--port 8123" in joined
    assert argv.count("--envelope-writable") == 2
    assert "--no-staging-gate" in argv
    assert "--shell" in argv
    # loopback only — the gateway must not be reachable off-host
    assert "--host 127.0.0.1" in joined


def test_caller_env_repoints_home_and_exports_coordinates(tmp_path):
    mapping = {"MCP_URL": "http://u", "MCP_TOKEN": "t", "MCP_CONFIG": "/c",
               "WORKSPACE": "/ws"}
    env = caller_env({"PATH": "/bin"}, tmp_path / "scratch", mapping)
    assert env["HOME"] == str(tmp_path / "scratch")
    assert env["BOUNDARY_MCP_URL"] == "http://u"
    assert env["BOUNDARY_MCP_TOKEN"] == "t"
    assert env["PATH"] == "/bin"
    assert (tmp_path / "scratch" / "tmp").is_dir()


def test_launch_fails_closed_without_srt(monkeypatch, capsys):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    rc = launch(_args(), ["echo", "hi"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "srt is not installed" in err
    assert "--allow-uncontained" in err


def test_launch_requires_a_caller(capsys):
    rc = launch(_args(), [])
    assert rc == 2
    assert "no caller command" in capsys.readouterr().err


def test_launch_uncontained_runs_caller(monkeypatch, tmp_path, capsys):
    """--allow-uncontained: gateway comes up, placeholders substitute, the
    caller runs unjailed with the env coordinates set, exit code propagates.
    Also covers F13 (credential env stripped from the caller) and F17
    (scratch dir removed once launch returns)."""
    pytest.importorskip("mcp")  # the gateway subprocess serves HTTP via the mcp SDK
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    marker = tmp_path / "ran.json"
    # Read the mcp config's own content (not just its path) while the caller
    # runs — the scratch dir holding it is removed once launch() returns
    # (F17), so the path itself is no longer readable by the time this test
    # asserts on it.
    caller = [
        sys.executable, "-c",
        "import json,os,sys; cfg = json.load(open(sys.argv[1]));"
        " json.dump({'url': os.environ['BOUNDARY_MCP_URL'], 'cfg': cfg,"
        " 'home': os.environ['HOME'],"
        " 'github_token': os.environ.get('GITHUB_TOKEN')}, open(sys.argv[2], 'w'))",
        "{MCP_CONFIG}", str(marker),
    ]
    rc = launch(_args(workspace=str(tmp_path / "ws"), allow_uncontained=True), caller)
    assert rc == 0
    data = json.loads(marker.read_text())
    assert data["url"].startswith("http://127.0.0.1:")
    assert data["cfg"]["mcpServers"]["boundary"]["url"]
    assert "UNJAILED" in capsys.readouterr().err
    # F13: the caller must not see the parent's credential-shaped env vars.
    assert not data["github_token"]
    # F17: the scratch dir (held mcp.json with the bearer token) must be
    # removed once launch() returns, not leaked on disk.
    assert not Path(data["home"]).exists()
