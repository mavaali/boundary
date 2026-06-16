"""Unit tests for the commit-tool kill-list.

Covers:
- Envelope refuses commit tools when on_commit="refuse"
- Envelope allows commit tools when on_commit="allow" + tool in allowlist
- Envelope refuses commit tool when on_commit="allow" but tool NOT in allowlist
- on_commit="queue" sets commit_halt_flag and breaks the loop
- on_commit="ask" sets halt_flag
- Bash basename denylist: curl/wget/gh/az/mail/sendmail/osascript blocked
- git subcommand exception: git status allowed, git push blocked
- Bash basename denylist ignores env-var prefixes (FOO=bar curl ...)
- Bash basename denylist matches absolute paths (/usr/bin/curl)
- bash_commit tool routes through commit policy, not bash denylist
- ScheduleConfig.validate_commit_policy surfaces bad combos
- Reason field required for kind="commit"
- BASH_COMMIT_DENYLIST hard cap at 12 (guardrail test)
"""
from __future__ import annotations
import platform
import shutil
from pathlib import Path

import pytest

from boundary.envelope import (
    Envelope,
    EnvelopeEvent,
    _make_enforced_tool,
    _bash_command_is_commit,
    BASH_COMMIT_DENYLIST,
    _GIT_COMMIT_SUBCOMMANDS,
)
from boundary.tools.registry import Tool, ToolRegistry
from boundary.tools.workspace import Workspace
from boundary.tools.shell import register_shell_tools
from boundary.schedule import ScheduleConfig

SANDBOX_AVAILABLE = platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None


# ---------- helpers ----------------------------------------------------------

def _commit_tool(name: str = "send_email", side_effect_record: list | None = None) -> Tool:
    record = side_effect_record if side_effect_record is not None else []

    def _fn(to: str = "", body: str = "", reason: str = "") -> str:
        record.append({"to": to, "body": body})
        return f"sent to {to}"

    return Tool(
        name=name,
        description=f"COMMIT tool — send email to {{to}} with {{body}}",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "body": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["to", "body", "reason"],
        },
        fn=_fn,
        kind="commit",
    )


def _enforced(tool: Tool, envelope: Envelope, halt_flag=None, commit_halt_flag=None):
    counters: dict[str, int] = {}
    events: list[EnvelopeEvent] = []
    iter_ref = [1]
    halt_flag = halt_flag if halt_flag is not None else [False]
    commit_halt_flag = commit_halt_flag if commit_halt_flag is not None else [False]
    return _make_enforced_tool(
        tool, envelope, counters, events, iter_ref,
        halt_flag=halt_flag, commit_halt_flag=commit_halt_flag,
    ), counters, events, halt_flag, commit_halt_flag


# ---------- commit policy ----------------------------------------------------

def test_commit_refuse_blocks_call():
    side_effects: list = []
    env = Envelope(on_commit="refuse")
    enforced, counters, events, _, _ = _enforced(_commit_tool(side_effect_record=side_effects), env)
    r = enforced.call({"to": "a@b.com", "body": "hi", "reason": "test"})
    assert "ENVELOPE REFUSED" in r
    assert "commit tool" in r
    assert side_effects == []  # underlying fn never called
    assert counters.get("commit_attempted") == 1
    assert counters.get("commit_executed", 0) == 0
    assert any(e.kind == "commit_refused" for e in events)


def test_commit_allow_with_allowlist_executes():
    side_effects: list = []
    env = Envelope(on_commit="allow", commit_allowlist=["send_email"])
    enforced, counters, events, _, _ = _enforced(_commit_tool(side_effect_record=side_effects), env)
    r = enforced.call({"to": "a@b.com", "body": "hi", "reason": "test"})
    assert "sent to a@b.com" in r
    assert side_effects == [{"to": "a@b.com", "body": "hi"}]
    assert counters["commit_executed"] == 1
    assert any(e.kind == "commit_allowed" for e in events)


def test_commit_allow_without_allowlist_entry_blocks():
    side_effects: list = []
    env = Envelope(on_commit="allow", commit_allowlist=["other_tool"])
    enforced, counters, events, _, _ = _enforced(_commit_tool(side_effect_record=side_effects), env)
    r = enforced.call({"to": "a@b.com", "body": "hi", "reason": "test"})
    assert "ENVELOPE REFUSED" in r
    assert "commit_allowlist" in r
    assert side_effects == []
    assert counters.get("commit_executed", 0) == 0


def test_commit_allow_with_empty_allowlist_executes_all():
    """Empty allowlist under 'allow' = unrestricted. Documented foot-gun."""
    side_effects: list = []
    env = Envelope(on_commit="allow", commit_allowlist=[])
    enforced, _, _, _, _ = _enforced(_commit_tool(side_effect_record=side_effects), env)
    r = enforced.call({"to": "a@b.com", "body": "hi", "reason": "test"})
    assert "sent to" in r


def test_commit_queue_sets_commit_halt_flag():
    side_effects: list = []
    env = Envelope(on_commit="queue")
    enforced, counters, events, _, commit_halt_flag = _enforced(
        _commit_tool(side_effect_record=side_effects), env,
    )
    r = enforced.call({"to": "a@b.com", "body": "hi", "reason": "test"})
    assert "[HALTED]" in r
    assert commit_halt_flag[0] is True
    assert side_effects == []
    assert any(e.kind == "commit_halt" for e in events)


def test_commit_ask_sets_halt_flag():
    env = Envelope(on_commit="ask")
    enforced, _, events, halt_flag, _ = _enforced(_commit_tool(), env)
    r = enforced.call({"to": "a@b.com", "body": "hi", "reason": "test"})
    assert "[HALTED]" in r
    assert "ask_human" in r
    assert halt_flag[0] is True


def test_commit_missing_reason_refused():
    env = Envelope(on_commit="allow", commit_allowlist=["send_email"])
    enforced, _, events, _, _ = _enforced(_commit_tool(), env)
    r = enforced.call({"to": "a@b.com", "body": "hi"})  # no reason
    assert "ENVELOPE REFUSED" in r
    assert "reason" in r
    assert any(e.kind == "missing_reason" for e in events)


# ---------- bash basename denylist ------------------------------------------

@pytest.mark.parametrize("cmd,expected", [
    ("curl https://example.com -X POST", (True, "curl")),
    ("wget --post-data=foo http://x", (True, "wget")),
    ("gh issue create --title foo", (True, "gh")),
    ("az repos pr create", (True, "az")),
    ("mail user@x.com < body", (True, "mail")),
    ("sendmail -t < msg", (True, "sendmail")),
    ("osascript -e 'tell application Mail'", (True, "osascript")),
    ("git push origin main", (True, "git push")),
    ("git commit -m foo", (True, "git commit")),
    ("git tag v1.0", (True, "git tag")),
    # git read subcommands are allowed
    ("git status", (False, "")),
    ("git log --oneline", (False, "")),
    ("git diff", (False, "")),
    # benign commands pass
    ("ls -la", (False, "")),
    ("cat README.md", (False, "")),
    ("grep -r foo src/", (False, "")),
    # absolute path still matched by basename
    ("/usr/bin/curl https://x", (True, "curl")),
    # env-var prefix doesn't fool us
    ("FOO=bar curl https://x", (True, "curl")),
    ("FOO=bar BAZ=qux gh issue list", (True, "gh")),
    # empty / whitespace
    ("", (False, "")),
    ("   ", (False, "")),
])
def test_bash_command_is_commit(cmd, expected):
    assert _bash_command_is_commit(cmd) == expected


def test_bash_denylist_blocks_curl(tmp_path):
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_shell_tools(base, ws)
    env = Envelope()
    bash = base.get("bash")
    enforced, counters, events, _, _ = _enforced(bash, env)
    r = enforced.call({"command": "curl https://example.com", "reason": "test"})
    assert "ENVELOPE REFUSED" in r
    assert "curl" in r
    assert "bash_commit" in r
    assert any(e.kind == "bash_commit_blocked" for e in events)


def test_bash_allows_git_status(tmp_path):
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_shell_tools(base, ws)
    env = Envelope()
    bash = base.get("bash")
    enforced, _, events, _, _ = _enforced(bash, env)
    r = enforced.call({"command": "git --version", "reason": "test"})
    # Should NOT be commit-blocked (note: subprocess actually runs; that's fine)
    assert "ENVELOPE REFUSED" not in r or "denylist" not in r
    assert not any(e.kind == "bash_commit_blocked" for e in events)


@pytest.mark.skipif(not SANDBOX_AVAILABLE, reason="macOS sandbox-exec required")
def test_bash_can_write_inside_workspace(tmp_path):
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_shell_tools(base, ws)
    bash = base.get("bash")
    r = bash.call({"command": "echo ok > inside.txt && cat inside.txt", "reason": "test"})
    assert "[exit 0]" in r
    assert "ok" in r
    assert (tmp_path / "inside.txt").read_text().strip() == "ok"


@pytest.mark.skipif(not SANDBOX_AVAILABLE, reason="macOS sandbox-exec required")
def test_bash_cannot_write_outside_workspace(tmp_path):
    outside = tmp_path.parent / "outside-boundary-test.txt"
    if outside.exists():
        outside.unlink()
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_shell_tools(base, ws)
    bash = base.get("bash")
    r = bash.call({"command": f"echo no > {outside}", "reason": "test"})
    assert "[exit 1]" in r
    assert "Operation not permitted" in r
    assert not outside.exists()


def test_bash_commit_routes_through_commit_policy(tmp_path):
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_shell_tools(base, ws)
    env = Envelope(on_commit="refuse")
    bc = base.get("bash_commit")
    assert bc is not None and bc.kind == "commit"
    enforced, counters, events, _, _ = _enforced(bc, env)
    r = enforced.call({"command": "echo hi", "reason": "test"})
    assert "ENVELOPE REFUSED" in r
    assert counters.get("commit_executed", 0) == 0


def test_bash_commit_executes_when_allowed(tmp_path):
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_shell_tools(base, ws)
    env = Envelope(on_commit="allow", commit_allowlist=["bash_commit"])
    bc = base.get("bash_commit")
    enforced, counters, events, _, _ = _enforced(bc, env)
    r = enforced.call({"command": "echo hi", "reason": "test"})
    assert "[exit 0]" in r
    assert counters["commit_executed"] == 1
    # Also counts against writes_executed (it's still a side-effect command)
    assert counters.get("writes_executed", 0) == 1


# ---------- schedule validation ---------------------------------------------

def _write_schedule(tmp_path, **overrides) -> Path:
    import yaml
    body = {
        "name": "test",
        "schedule": "daily 09:00",
        "persona": "banner",
        "workspace": str(tmp_path),
        "task": "do a thing",
        "envelope": {"writable_paths": ["out.md"], "max_writes": 1, "min_writes": 1},
    }
    body.update(overrides)
    p = tmp_path / "sched.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def test_schedule_default_on_commit_is_refuse(tmp_path):
    cfg = ScheduleConfig.load(_write_schedule(tmp_path))
    assert cfg.on_commit == "refuse"
    assert cfg.commit_allowlist == []
    assert cfg.validate_commit_policy() == []


def test_schedule_explicit_queue_validates(tmp_path):
    cfg = ScheduleConfig.load(_write_schedule(tmp_path, on_commit="queue"))
    assert cfg.on_commit == "queue"
    assert cfg.validate_commit_policy() == []


def test_schedule_allow_empty_allowlist_warns(tmp_path):
    cfg = ScheduleConfig.load(_write_schedule(tmp_path, on_commit="allow"))
    errs = cfg.validate_commit_policy()
    assert errs and "empty commit_allowlist" in errs[0]


def test_schedule_allow_with_allowlist_validates(tmp_path):
    cfg = ScheduleConfig.load(
        _write_schedule(tmp_path, on_commit="allow", commit_allowlist=["bash_commit"]),
    )
    assert cfg.validate_commit_policy() == []


def test_schedule_allowlist_without_allow_warns(tmp_path):
    cfg = ScheduleConfig.load(
        _write_schedule(tmp_path, commit_allowlist=["bash_commit"]),  # on_commit defaults to refuse
    )
    errs = cfg.validate_commit_policy()
    assert errs and "only consulted under on_commit: allow" in errs[0]


def test_schedule_invalid_on_commit_rejected(tmp_path):
    cfg = ScheduleConfig.load(_write_schedule(tmp_path, on_commit="yolo"))
    errs = cfg.validate_commit_policy()
    assert errs and "refuse|queue|allow" in errs[0]


# ---------- guardrails ------------------------------------------------------

def test_bash_commit_denylist_hard_cap():
    """Hard cap of 12 entries. Frozen at 8 today. If you're here adding entry
    13, STOP — build a typed kind='commit' tool instead."""
    assert len(BASH_COMMIT_DENYLIST) <= 12, (
        "BASH_COMMIT_DENYLIST exceeded the 12-entry cap. Build a typed "
        "kind='commit' tool instead of extending the denylist."
    )
    # And it shouldn't shrink below the frozen-8 baseline either.
    assert {"curl", "wget", "gh", "az", "mail", "sendmail", "osascript", "git"}.issubset(
        set(BASH_COMMIT_DENYLIST)
    )


def test_git_commit_subcommands_are_minimal():
    """Only push/commit/tag. If you add a fourth, it's signal to rethink the
    git-subcommand exception entirely."""
    assert _GIT_COMMIT_SUBCOMMANDS == frozenset({"push", "commit", "tag"})
