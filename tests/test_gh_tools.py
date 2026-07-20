"""Typed `gh` commit tools (v3 Item 2).

`gh` sits on the bash denylist, so raw `gh` via bash is refused as commit-class.
The sanctioned path (named in the denylist comment itself) is a typed
`kind="commit"` tool governed by the envelope's `on_commit` policy — not a
longer denylist. `gh_pr_create` appends the run's policy attestation
(`boundary.receipt/v1`) to the PR body so an agent-authored PR is self-describing.
"""
from __future__ import annotations

import json

import boundary.tools.gh as gh_mod
from boundary.envelope import BASH_COMMIT_DENYLIST, Envelope, EnvelopeEvent, _make_enforced_tool
from boundary.tools.gh import GH_VERB_CAP, register_gh_tools
from boundary.tools.registry import ToolRegistry
from boundary.tools.workspace import Workspace


class _Capture:
    """Stand-in for run_sandboxed: record the command, never touch the network."""
    def __init__(self):
        self.commands = []

    def __call__(self, command, **kw):
        self.commands.append(command)
        return "OK (captured)"


def _registry(tmp_path, monkeypatch, *, policy_provider=None):
    cap = _Capture()
    monkeypatch.setattr(gh_mod, "run_sandboxed", cap)
    reg = ToolRegistry()
    register_gh_tools(reg, Workspace(root=tmp_path), policy_provider=policy_provider)
    return reg, cap


def _enforce(reg, env):
    counters: dict[str, int] = {}
    events: list[EnvelopeEvent] = []
    out = ToolRegistry()
    for tool in reg._tools.values():
        out.register(_make_enforced_tool(tool, env, counters, events, [1],
                                         halt_flag=[False], commit_halt_flag=[False]))
    return out, counters, events


def test_gh_tools_are_commit_kind(tmp_path, monkeypatch):
    reg, _ = _registry(tmp_path, monkeypatch)
    assert reg.get("gh_pr_create").kind == "commit"
    assert reg.get("gh_issue_comment").kind == "commit"


def test_gh_pr_create_refused_under_on_commit_refuse(tmp_path, monkeypatch):
    reg, cap = _registry(tmp_path, monkeypatch)
    env = Envelope(on_commit="refuse")
    env.require_staging = False
    enforced, counters, events = _enforce(reg, env)
    r = enforced.get("gh_pr_create").call(
        {"title": "T", "body": "B", "reason": "redteam"})
    assert "ENVELOPE REFUSED" in r
    assert cap.commands == []  # side effect blocked
    assert counters.get("commit_executed", 0) == 0
    assert any(e.kind == "commit_refused" for e in events)


def test_gh_pr_create_executes_under_allow_and_carries_receipt(tmp_path, monkeypatch):
    env = Envelope(writable_paths=["out.md"], on_commit="allow",
                   commit_allowlist=["gh_pr_create"])
    env.require_staging = False
    reg, cap = _registry(tmp_path, monkeypatch,
                         policy_provider=lambda: {"spec_hash": env.spec_hash(),
                                                  "spec": env.spec_dict()})
    enforced, counters, events = _enforce(reg, env)
    r = enforced.get("gh_pr_create").call(
        {"title": "Add feature", "body": "Does the thing.", "reason": "ship it"})
    assert "captured" in r
    assert len(cap.commands) == 1
    cmd = cap.commands[0]
    assert cmd.startswith("gh pr create")
    assert "Add feature" in cmd
    # the receipt attestation is embedded in the body
    assert "boundary.receipt/v1" in cmd
    assert env.spec_hash() in cmd
    assert any(e.kind == "commit_allowed" for e in events)


def test_gh_pr_create_not_in_allowlist_is_refused(tmp_path, monkeypatch):
    reg, cap = _registry(tmp_path, monkeypatch)
    env = Envelope(on_commit="allow", commit_allowlist=["gh_issue_comment"])
    env.require_staging = False
    enforced, _, _ = _enforce(reg, env)
    r = enforced.get("gh_pr_create").call({"title": "T", "body": "B", "reason": "x"})
    assert "ENVELOPE REFUSED" in r
    assert cap.commands == []


def test_gh_issue_comment_executes_and_quotes_args(tmp_path, monkeypatch):
    env = Envelope(on_commit="allow", commit_allowlist=["gh_issue_comment"])
    env.require_staging = False
    reg, cap = _registry(tmp_path, monkeypatch)
    enforced, _, _ = _enforce(reg, env)
    # A body with shell metacharacters must be quoted, not interpolated raw.
    r = enforced.get("gh_issue_comment").call(
        {"issue": "42", "body": "done; rm -rf /", "reason": "note"})
    assert "captured" in r
    cmd = cap.commands[0]
    assert cmd.startswith("gh issue comment")
    assert "42" in cmd
    # the dangerous substring appears only inside a quoted argument
    assert "'done; rm -rf /'" in cmd or '"done; rm -rf /"' in cmd


def test_receipt_block_is_valid_json_with_pending_verdict(tmp_path, monkeypatch):
    env = Envelope(writable_paths=["out.md"], on_commit="allow",
                   commit_allowlist=["gh_pr_create"])
    env.require_staging = False
    reg, cap = _registry(tmp_path, monkeypatch,
                         policy_provider=lambda: {"spec_hash": env.spec_hash(),
                                                  "spec": env.spec_dict()})
    enforced, _, _ = _enforce(reg, env)
    enforced.get("gh_pr_create").call({"title": "T", "body": "B", "reason": "x"})
    cmd = cap.commands[0]
    block = cmd.split("```json", 1)[1].split("```", 1)[0]
    doc = json.loads(block)
    assert doc["schema"] == "boundary.receipt/v1"
    assert doc["spec_hash"] == env.spec_hash()
    assert doc["verdict"] is None  # not graded yet at commit time


def test_denylist_unchanged_and_gh_still_blocked_raw(tmp_path, monkeypatch):
    # Adding typed tools must NOT grow the denylist; raw `gh` via bash stays blocked.
    assert len(BASH_COMMIT_DENYLIST) == 8
    assert "gh" in BASH_COMMIT_DENYLIST


def test_verb_cap_is_documented_and_respected(tmp_path, monkeypatch):
    reg, _ = _registry(tmp_path, monkeypatch)
    gh_tools = [n for n in reg._tools if n.startswith("gh_")]
    assert len(gh_tools) <= GH_VERB_CAP


def test_agent_enable_gh_registers_tools(tmp_path):
    from boundary.agent import Agent

    class _Client:
        model = "claude-haiku-4.5"
        def chat(self, *a, **k): raise AssertionError("not called")

    agent = Agent(name="a", system_prompt="x", workspace=str(tmp_path), client=_Client(),
                  enable_fs=False, enable_shell=False, enable_web=False,
                  enable_gh=True, transcript=False)
    assert "gh_pr_create" in agent.tools
    assert "gh_issue_comment" in agent.tools
    assert agent.gh_policy is None  # unbound until a runner supplies the envelope


def test_agent_without_enable_gh_has_no_gh_tools(tmp_path):
    from boundary.agent import Agent

    class _Client:
        model = "claude-haiku-4.5"
        def chat(self, *a, **k): raise AssertionError("not called")

    agent = Agent(name="a", system_prompt="x", workspace=str(tmp_path), client=_Client(),
                  enable_fs=False, enable_shell=False, enable_web=False, transcript=False)
    assert "gh_pr_create" not in agent.tools


def test_runner_binds_live_policy_into_attestation(tmp_path, monkeypatch):
    """The receipt attestation must carry the spec_hash of the envelope that is
    actually governing the run — bound by the runner, not at registration."""
    from boundary.agent import Agent
    from boundary.envelope import EnvelopeRunner

    cap = _Capture()
    monkeypatch.setattr(gh_mod, "run_sandboxed", cap)

    class _Client:
        model = "claude-haiku-4.5"
        def __init__(self): self.n = 0
        def chat(self, messages, tools=None, **kw):
            from boundary.clients.base import ChatResponse, Message, ToolCall
            self.n += 1
            if self.n == 1:
                tc = ToolCall(id="c1", name="gh_pr_create",
                              arguments={"title": "T", "body": "B", "reason": "ship"})
                return ChatResponse(message=Message(role="assistant", content="", tool_calls=[tc]),
                                    finish_reason="tool_calls", input_tokens=1,
                                    output_tokens=1, cached_input_tokens=0)
            return ChatResponse(message=Message(role="assistant", content="[DATA] done"),
                                finish_reason="stop", input_tokens=1, output_tokens=1,
                                cached_input_tokens=0)

    agent = Agent(name="a", system_prompt="x", workspace=str(tmp_path), client=_Client(),
                  enable_fs=False, enable_shell=False, enable_web=False,
                  enable_gh=True, transcript=False, max_iters=3)
    env = Envelope(writable_paths=["out.md"], require_staging=False, repeat_halt=0,
                   on_commit="allow", commit_allowlist=["gh_pr_create"])
    EnvelopeRunner(agent, env).run("task")

    assert len(cap.commands) == 1
    assert env.spec_hash() in cap.commands[0]
    assert agent.gh_policy["spec_hash"] == env.spec_hash()


def test_schedule_yaml_opt_in_parses(tmp_path):
    """gh tools are opt-in per schedule; default off. Opting in only REGISTERS
    them — on_commit still governs whether they can execute."""
    import yaml

    from boundary.schedule import ScheduleConfig

    base = {"name": "s", "schedule": "daily 09:00", "persona": "p",
            "workspace": "/ws", "task": "t"}

    def _load(extra):
        p = tmp_path / f"s{len(extra)}.yaml"
        p.write_text(yaml.safe_dump({**base, **extra}), encoding="utf-8")
        return ScheduleConfig.load(p)

    assert _load({}).enable_gh is False
    assert _load({"enable_gh": True}).enable_gh is True
