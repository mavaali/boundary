"""Unit tests for D: no-progress/repeated-action halt + early-stop nudge.

Both behaviors live in EnvelopeRunner.run(), so these drive the full loop with a
scripted fake client rather than the enforced-tool layer.
"""
from __future__ import annotations

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeRunner
from boundary.tools.registry import Tool


class _ScriptClient:
    """Emits one tool call per chat() from a fixed script; 'stop' when exhausted."""
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


def _noop_tool():
    def noop(x: str = "") -> str:
        return "constant-result"
    return Tool(name="noop", description="x",
                parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": []},
                fn=noop, kind="read")


def _agent(ws, client):
    a = Agent(name="d", system_prompt="x", workspace=str(ws), client=client,
              enable_fs=True, enable_shell=False, enable_web=False, transcript=False)
    a.tools.register(_noop_tool())
    return a


def test_repeated_action_triggers_no_progress_halt(tmp_path):
    # 4 identical calls; with repeat_halt=3 the run must halt on the 3rd.
    client = _ScriptClient([("noop", {"x": "same"})] * 4)
    agent = _agent(tmp_path, client)
    env = Envelope(writable_paths=["out.md"], require_staging=False,
                   repeat_warn=2, repeat_halt=3)
    res = EnvelopeRunner(agent, env).run("go")
    assert res.loop_result.stop_reason == "no_progress_halt"
    assert any(e.kind == "no_progress" for e in res.events)
    # Halted on the 3rd identical call, not after exhausting the script of 4.
    assert res.loop_result.iterations == 3


def test_varied_actions_do_not_halt(tmp_path):
    # Distinct args each time → no repeated-action halt.
    client = _ScriptClient([("noop", {"x": f"v{i}"}) for i in range(4)])
    agent = _agent(tmp_path, client)
    env = Envelope(writable_paths=["out.md"], require_staging=False,
                   repeat_warn=2, repeat_halt=3)
    res = EnvelopeRunner(agent, env).run("go")
    assert res.loop_result.stop_reason != "no_progress_halt"
    assert not any(e.kind == "no_progress" for e in res.events)


class _StopThenWrite:
    """Premature stop on call 1; a real write on call 2 (post-nudge)."""
    model = "claude-sonnet-4.6"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, **kw):
        from boundary.clients.base import ChatResponse, Message, ToolCall
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(message=Message(role="assistant", content="I'm done."),
                                finish_reason="stop", input_tokens=1, output_tokens=1, cached_input_tokens=0)
        if self.calls == 2:
            tc = ToolCall(id="w1", name="write_file",
                          arguments={"path": "out.md", "content": "x", "reason": "after nudge"})
            return ChatResponse(message=Message(role="assistant", content="", tool_calls=[tc]),
                                finish_reason="tool_calls", input_tokens=1, output_tokens=1, cached_input_tokens=0)
        return ChatResponse(message=Message(role="assistant", content="done"),
                            finish_reason="stop", input_tokens=1, output_tokens=1, cached_input_tokens=0)


def test_early_stop_nudge_converts_premature_stop_to_write(tmp_path):
    client = _StopThenWrite()
    agent = _agent(tmp_path, client)
    env = Envelope(writable_paths=["out.md"], require_staging=False,
                   min_writes=1, nudge_on_early_stop=True)
    res = EnvelopeRunner(agent, env).run("go")
    assert any(e.kind == "early_stop_nudge" for e in res.events)
    # The nudge must fire at most once.
    assert sum(1 for e in res.events if e.kind == "early_stop_nudge") == 1
    # And it converted the premature stop into the required write.
    assert res.writes_executed == 1
    assert (tmp_path / "out.md").read_text() == "x"


def test_no_nudge_when_min_writes_already_met(tmp_path):
    # Write first (min_writes satisfied), then stop → no nudge (bounded, not maximal).
    client = _ScriptClient([("write_file", {"path": "out.md", "content": "x", "reason": "t"})])
    agent = _agent(tmp_path, client)
    env = Envelope(writable_paths=["out.md"], require_staging=False, min_writes=1,
                   nudge_on_early_stop=True)
    res = EnvelopeRunner(agent, env).run("go")
    assert not any(e.kind == "early_stop_nudge" for e in res.events)
    assert res.writes_executed == 1
