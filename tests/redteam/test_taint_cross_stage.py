"""Headline locks: taint must survive the pipeline-stage / invocation boundary.

Stage 1 fetches untrusted content and writes it into the shared workspace.
Stage 2 is a SEPARATE EnvelopeRunner.run() (fresh counters) that reads that file
and attempts a write/commit. Without the persisted ledger, taint would reset
between runs; with it, the taint must carry across.
"""
from __future__ import annotations
import json

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeRunner
from boundary.taint import TaintStore
from boundary.tools.registry import Tool


class _ScriptClient:
    """Emits a fixed sequence of tool calls, one per chat() call."""
    model = "claude-sonnet-4.6"

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def chat(self, messages, tools=None, **kw):
        from boundary.clients.base import ChatResponse, Message, ToolCall
        if self.i >= len(self.script):
            return ChatResponse(message=Message(role="assistant", content="done"),
                                finish_reason="stop", input_tokens=1, output_tokens=1, cached_input_tokens=0)
        name, args = self.script[self.i]; self.i += 1
        tc = ToolCall(id=f"c{self.i}", name=name, arguments=args)
        return ChatResponse(message=Message(role="assistant", content="", tool_calls=[tc]),
                            finish_reason="tool_calls", input_tokens=1, output_tokens=1, cached_input_tokens=0)


def _stub_fetch():
    def fetch_url(url: str, reason: str = "") -> str:
        return f"<untrusted {url}>"   # no live network — keeps the test hermetic
    return Tool(name="fetch_url", description="x",
                parameters={"type": "object",
                            "properties": {"url": {"type": "string"}, "reason": {"type": "string"}},
                            "required": ["url", "reason"]},
                fn=fetch_url, kind="external")


def _agent(ws, client, driver="seatbelt"):
    a = Agent(name="s", system_prompt="x", workspace=str(ws), client=client,
              enable_fs=True, enable_shell=False, enable_web=False,
              sandbox_driver=driver, transcript=True)
    a.tools.register(_stub_fetch())
    return a


def test_cross_invocation_commit_gate_refuse(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    ws = tmp_path / "ws"; ws.mkdir()

    s1 = _agent(ws, _ScriptClient([
        ("fetch_url", {"url": "http://evil.test", "reason": "scout"}),
        ("write_file", {"path": "intel/raw.md", "content": "hostile", "reason": "dump"}),
    ]))
    EnvelopeRunner(s1, Envelope(writable_paths=["intel/*"], require_staging=False, on_taint="warn")).run("scout")
    assert TaintStore.load(ws).is_tainted("intel/raw.md")

    s2 = _agent(ws, _ScriptClient([
        ("read_file", {"path": "intel/raw.md"}),
        ("write_file", {"path": "memo.md", "content": "leak", "reason": "synth"}),
    ]))
    res = EnvelopeRunner(s2, Envelope(writable_paths=["memo.md"], require_staging=False, on_taint="refuse")).run("synth")
    assert res.writes_executed == 0
    assert not (ws / "memo.md").exists()
    assert any(e.kind == "taint_flow" for e in res.events)


def test_cross_invocation_warn_surfaces_in_umpire(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    ws = tmp_path / "ws"; ws.mkdir()
    s1 = _agent(ws, _ScriptClient([
        ("fetch_url", {"url": "http://evil.test", "reason": "r"}),
        ("write_file", {"path": "intel/raw.md", "content": "h", "reason": "r"}),
    ]))
    EnvelopeRunner(s1, Envelope(writable_paths=["intel/*"], require_staging=False, on_taint="warn")).run("scout")

    s2 = _agent(ws, _ScriptClient([
        ("read_file", {"path": "intel/raw.md"}),
        ("write_file", {"path": "memo.md", "content": "x", "reason": "r"}),
    ]))
    res = EnvelopeRunner(s2, Envelope(writable_paths=["memo.md"], require_staging=False, on_taint="warn")).run("synth")
    assert res.writes_executed == 1
    assert any(e.kind == "taint_flow" for e in res.events)

    from boundary.third_umpire import ThirdUmpire
    report = ThirdUmpire.grade(s2.transcript.path)
    assert report.verdict == "FAIL"
    assert any(c.name == "egress_uncontained" for c in report.checks)
