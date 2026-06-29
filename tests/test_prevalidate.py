"""Unit + integration tests for B: pre-exec validity gate (required-fields only)."""
from __future__ import annotations

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeRunner, _prevalidate_call, classify_tool_result
from boundary.tools.registry import Tool


def _tool(required):
    return Tool(
        name="t", description="x",
        parameters={"type": "object",
                    "properties": {f: {"type": "string"} for f in required},
                    "required": required},
        fn=lambda **k: "ok", kind="write",
    )


def test_missing_required_field_rejected():
    msg = _prevalidate_call(_tool(["path", "content", "reason"]), {"path": "x"})
    assert msg is not None
    assert "content" in msg
    # 'reason' is a policy concern, never flagged by the arg gate.
    assert "reason" not in msg


def test_all_required_present_passes():
    assert _prevalidate_call(_tool(["path", "content", "reason"]),
                             {"path": "x", "content": "y", "reason": "z"}) is None


def test_reason_only_missing_is_not_arg_invalid():
    # Only 'reason' missing → gate must pass (policy layer handles reason).
    assert _prevalidate_call(_tool(["path", "reason"]), {"path": "x"}) is None


def test_none_value_counts_as_missing():
    msg = _prevalidate_call(_tool(["path"]), {"path": None})
    assert msg is not None and "path" in msg


def test_no_required_fields_passes():
    assert _prevalidate_call(_tool([]), {}) is None


def test_prevalidate_message_classifies_arg_invalid():
    msg = _prevalidate_call(_tool(["path", "content"]), {"path": "x"})
    assert classify_tool_result(msg) == "arg-invalid"


class _ScriptClient:
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


def test_gate_skips_execution_no_side_effect(tmp_path):
    # write_file missing 'content' → gate fires, file is never created, and the
    # enforced wrapper never runs (writes_attempted stays 0).
    client = _ScriptClient([("write_file", {"path": "out.md", "reason": "t"})])
    agent = Agent(name="b", system_prompt="x", workspace=str(tmp_path), client=client,
                  enable_fs=True, enable_shell=False, enable_web=False, transcript=False)
    env = Envelope(writable_paths=["out.md"], require_staging=False, repeat_halt=0)
    res = EnvelopeRunner(agent, env).run("go")
    assert not (tmp_path / "out.md").exists()
    assert res.writes_attempted == 0
    assert res.results_by_class.get("arg-invalid", 0) >= 1
