"""Unit + integration tests for A: typed tool-result feedback (4 categories)."""
from __future__ import annotations

import pytest

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeRunner, classify_tool_result


@pytest.mark.parametrize("result,expected", [
    ("wrote 5 chars to out.md", "success"),
    ("appended 3 chars to out.md", "success"),
    ("[exit 0]\nhello", "success"),
    ("just some file contents", "success"),
    ("STAGED: thesis recorded", "success"),
    ("[exit 1]\nboom", "runtime-error"),
    ("[UNSANDBOXED — no OS write-jail or egress boundary]\n[exit 2]\nx", "runtime-error"),
    ("ERROR: command timed out after 60s", "runtime-error"),
    ("ERROR: unknown sandbox driver 'foo'", "runtime-error"),
    ("ERROR: file not found: x", "arg-invalid"),
    ("ERROR: not a regular file: x", "arg-invalid"),
    ("ERROR: old_str not found", "arg-invalid"),
    ("ERROR: old_str matches 3 times; needs to be unique", "arg-invalid"),
    ("ERROR: unknown tool frobnicate", "arg-invalid"),
    ("ENVELOPE REFUSED: max_writes (1) reached.", "policy-refused"),
    ("ENVELOPE REFUSED: orientation reads are exhausted. Call `stage_proposal`", "policy-refused"),
    ("[HALTED] Commit tool 'bash_commit' requires human approval.", "policy-refused"),
])
def test_classify_strings(result, expected):
    assert classify_tool_result(result) == expected


def test_classify_raised_exceptions():
    assert classify_tool_result("ERROR: TypeError: x", TypeError("x")) == "arg-invalid"
    assert classify_tool_result("ERROR: KeyError: x", KeyError("x")) == "arg-invalid"
    assert classify_tool_result("ERROR: RuntimeError: x", RuntimeError("x")) == "runtime-error"


def test_generic_error_is_runtime():
    # An ERROR: string not matching an arg-invalid/runtime prefix falls to runtime.
    assert classify_tool_result("ERROR: something odd happened") == "runtime-error"


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


def test_results_by_class_populated_on_run(tmp_path):
    # One successful write + one unknown-tool call (arg-invalid).
    client = _ScriptClient([
        ("write_file", {"path": "out.md", "content": "x", "reason": "t"}),
        ("frobnicate", {}),
    ])
    agent = Agent(name="a", system_prompt="x", workspace=str(tmp_path), client=client,
                  enable_fs=True, enable_shell=False, enable_web=False, transcript=False)
    env = Envelope(writable_paths=["out.md"], require_staging=False, repeat_halt=0)
    res = EnvelopeRunner(agent, env).run("go")
    assert res.results_by_class.get("success", 0) >= 1
    assert res.results_by_class.get("arg-invalid", 0) >= 1
