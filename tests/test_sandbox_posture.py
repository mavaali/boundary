"""Regression guards for the opt-in sandbox-posture hardening from the audit.

F6 — under srt, allowRead was ["/"] and bash could read any file the invoking
     user could (~/.aws, ~/.ssh, ...). An opt-in read denylist (deny_read +
     deny_read_secrets) now populates srt's denyRead. Only enforceable under srt;
     other drivers warn.
F7 — the commit-class bash denylist is a nudge, not a boundary, and seatbelt/none
     do not bound egress. An opt-in Envelope.require_srt_for_bash refuses bash
     unless the resolved driver is srt.
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path

from boundary.envelope import Envelope, _make_enforced_tool
from boundary.schedule import ScheduleConfig
from boundary.tools.registry import Tool
from boundary.tools.sandbox import _srt_settings, default_deny_read, run_sandboxed

# --- F6: read denylist --------------------------------------------------------


def test_default_deny_read_covers_common_secrets():
    d = default_deny_read()
    # normalize separators: Path.home() joins render with '\' on Windows.
    joined = "\n".join(d).replace("\\", "/")
    for needle in [".aws", ".ssh", ".config/gh", "/etc/shadow"]:
        assert needle in joined


def test_srt_settings_wires_deny_read():
    s = _srt_settings(Path("/tmp/ws"), ["example.com"], ["/home/u/.aws", "/etc/shadow"])
    assert s["filesystem"]["denyRead"] == ["/home/u/.aws", "/etc/shadow"]
    # No allowRead: it takes precedence over denyRead in srt, so an allowRead:["/"]
    # would re-allow the secrets the denylist just denied (#55 / F26). Omitting it
    # is what makes the F6 read denylist actually bind.
    assert "allowRead" not in s["filesystem"]
    # allowWrite is str(root); normalize since Windows renders '\tmp\ws'.
    assert s["filesystem"]["allowWrite"][0].replace("\\", "/") == "/tmp/ws"


def test_deny_read_on_non_srt_driver_warns(tmp_path):
    import boundary.tools.sandbox as sb
    sb._WARNED_MESSAGES.clear()  # warn_once dedupes per message; reset for determinism
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        # driver=none so nothing actually runs a jail; we only assert the warning.
        run_sandboxed("echo hi", workspace_root=tmp_path, timeout=5,
                      driver="none", deny_read=["/secret"])
    out = buf.getvalue()
    assert "deny_read" in out and "cannot restrict reads" in out


def test_schedule_effective_deny_read_prepends_secrets_when_enabled():
    cfg = ScheduleConfig(
        name="n", schedule="daily@09:00", persona="p", workspace="/tmp/ws",
        task="t", deny_read=["/custom/path"], deny_read_secrets=True,
    )
    eff = cfg.effective_deny_read()
    assert "/custom/path" in eff
    assert any(".aws" in p for p in eff)  # defaults present
    # without the flag, only the custom entries
    cfg2 = ScheduleConfig(
        name="n", schedule="daily@09:00", persona="p", workspace="/tmp/ws",
        task="t", deny_read=["/custom/path"], deny_read_secrets=False,
    )
    assert cfg2.effective_deny_read() == ["/custom/path"]


# --- F7: require_srt_for_bash -------------------------------------------------


def _bash_tool():
    return Tool(
        "bash", "d",
        {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        lambda command, reason="": "[exit 0]\nran", kind="write",
    )


def _run_bash(driver, require):
    env = Envelope(writable_paths=["out.md"], require_staging=False, require_srt_for_bash=require)
    enforced = _make_enforced_tool(_bash_tool(), env, {}, events := [], [0], sandbox_driver=driver)
    return enforced.fn(command="echo hi", reason="x"), events


def test_require_srt_refuses_bash_on_non_srt_drivers():
    for driver in ["seatbelt", "none", "auto"]:
        out, events = _run_bash(driver, require=True)
        assert out.startswith("ENVELOPE REFUSED: this run sets require_srt_for_bash"), driver
        assert any(e.kind == "bash_refused" for e in events)


def test_require_srt_allows_bash_under_srt():
    out, _ = _run_bash("srt", require=True)
    assert not out.startswith("ENVELOPE REFUSED")
    assert out.strip().endswith("ran")


def test_default_off_allows_bash_on_seatbelt():
    out, _ = _run_bash("seatbelt", require=False)
    assert not out.startswith("ENVELOPE REFUSED")
    assert out.strip().endswith("ran")
