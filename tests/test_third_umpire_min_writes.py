"""Liveness grading: produced_output grades against min_writes, not just > 0.

The envelope hard-enforces the write CEILING (max_writes) at the tool layer, but
the write FLOOR (min_writes) was only ever a soft in-loop nudge. Boundary's thesis
is "we don't prevent, we grade against the spec" — so the floor belongs in the
Third Umpire's verdict: a run that under-delivers (writes_executed < min_writes)
should FAIL produced_output, not pass because it managed a single write.

Backward-compatible: a transcript with no min_writes recorded defaults to 1, which
reproduces the old `> 0` behaviour exactly.
"""
from __future__ import annotations

import json

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeRunner
from boundary.third_umpire import ThirdUmpire
from boundary.transcript import Transcript


def _tx(tmp_path, *, min_writes, writes, log_min_writes=True):
    """A minimal graded transcript with a declared min_writes floor."""
    start = {"type": "envelope_start", "writable_paths": ["out.md"],
             "require_staging": False, "max_writes": 10}
    if log_min_writes:
        start["min_writes"] = min_writes
    records = [
        start,
        {"type": "assistant", "iteration": 1, "content": "[DATA] done", "tool_calls": []},
        {"type": "envelope_end", "writes_attempted": writes, "writes_executed": writes,
         "external_calls": 0, "commit_attempted": 0, "commit_executed": 0,
         "input_tokens": 1000, "output_tokens": 500, "estimated_dollars": 0.01,
         "events": []},
        {"type": "end", "iterations": 2},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _produced(report):
    return next(c for c in report.checks if c.name == "produced_output")


def test_under_min_writes_fails(tmp_path):
    # min_writes=2 but only 1 write executed -> under-delivered -> FAIL.
    c = _produced(ThirdUmpire.grade(_tx(tmp_path, min_writes=2, writes=1)))
    assert c.passed is False
    assert c.severity == "fail"
    assert "min_writes=2" in c.detail


def test_meets_min_writes_passes(tmp_path):
    c = _produced(ThirdUmpire.grade(_tx(tmp_path, min_writes=2, writes=2)))
    assert c.passed is True


def test_zero_writes_still_fails(tmp_path):
    c = _produced(ThirdUmpire.grade(_tx(tmp_path, min_writes=1, writes=0)))
    assert c.passed is False
    assert c.severity == "fail"


def test_missing_min_writes_defaults_to_one(tmp_path):
    # Old transcript without a recorded min_writes: 1 write passes (>= default 1).
    c = _produced(ThirdUmpire.grade(_tx(tmp_path, min_writes=1, writes=1, log_min_writes=False)))
    assert c.passed is True


class _ScriptClient:
    model = "claude-sonnet-4.6"

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def chat(self, messages, tools=None, **kw):
        from boundary.clients.base import ChatResponse, Message, ToolCall
        if self.i >= len(self.script):
            return ChatResponse(message=Message(role="assistant", content="done"),
                                finish_reason="stop", input_tokens=1, output_tokens=1,
                                cached_input_tokens=0)
        name, args = self.script[self.i]; self.i += 1
        tc = ToolCall(id=f"c{self.i}", name=name, arguments=args)
        return ChatResponse(message=Message(role="assistant", content="", tool_calls=[tc]),
                            finish_reason="tool_calls", input_tokens=1, output_tokens=1,
                            cached_input_tokens=0)


def test_envelope_start_logs_min_writes(tmp_path):
    # The grading half is useless if the run never records the floor it ran under.
    tpath = tmp_path / "run.jsonl"
    client = _ScriptClient([("write_file", {"path": "out.md", "content": "x", "reason": "t"})])
    agent = Agent(name="a", system_prompt="x", workspace=str(tmp_path), client=client,
                  enable_fs=True, enable_shell=False, enable_web=False,
                  transcript=Transcript(path=tpath))
    env = Envelope(writable_paths=["out.md"], require_staging=False, repeat_halt=0, min_writes=2)
    EnvelopeRunner(agent, env).run("go")
    starts = [json.loads(line) for line in tpath.read_text().splitlines()
              if '"envelope_start"' in line]
    assert starts and starts[0].get("min_writes") == 2
