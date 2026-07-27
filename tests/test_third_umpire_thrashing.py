"""Thrashing grading: what share of a run's tool calls went nowhere?

Typed feedback labels every tool result success | arg-invalid | policy-refused |
runtime-error, but until now nothing graded the mix. A run could satisfy every
hard gate and still be mostly noise — burn 12 of 16 calls on malformed or refused
actions, land its one write, and grade PASS.

This grades the ratio, not the count, so long healthy runs aren't punished for
scale, and it needs a minimum sample so a 2-call run can't trip it.

Distinct from the in-band no-progress halt (envelope.py feature D), which trips on
the SAME call repeated verbatim. An agent failing twelve different ways never
trips that, but is thrashing just as hard.
"""
from __future__ import annotations

import json

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeRunner
from boundary.third_umpire import THRASH_MIN_RESULTS, THRASH_RATIO, ThirdUmpire
from boundary.transcript import Transcript


def _tx(tmp_path, classes, *, log_class=True):
    """A graded transcript whose tool results carry the given result classes."""
    records = [
        {"type": "envelope_start", "writable_paths": ["out.md"],
         "require_staging": False, "max_writes": 10, "min_writes": 1},
        {"type": "assistant", "iteration": 1, "content": "[DATA] done", "tool_calls": []},
    ]
    for i, rc in enumerate(classes):
        r = {"type": "tool_result", "iteration": 1, "tool": "read_file",
             "tool_call_id": f"c{i}", "result": "x"}
        if log_class:
            r["result_class"] = rc
        records.append(r)
    records += [
        {"type": "envelope_end", "writes_attempted": 1, "writes_executed": 1,
         "external_calls": 0, "commit_attempted": 0, "commit_executed": 0,
         "input_tokens": 1000, "output_tokens": 500, "estimated_dollars": 0.01,
         # A real write event, so budget_pacing/produced_output pass on their own
         # merits and any FAIL in these tests is attributable to thrashing alone.
         "events": [{"kind": "write_allowed", "tool": "write_file",
                     "detail": "out.md", "iteration": 1}]},
        {"type": "end", "iterations": 2},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _thrash(report):
    return next((c for c in report.checks if c.name == "thrashing"), None)


def test_healthy_run_passes(tmp_path):
    # 1 refusal in 8 — the dominant real-corpus shape (ratio 0.125).
    c = _thrash(ThirdUmpire.grade(_tx(tmp_path, ["success"] * 7 + ["policy-refused"])))
    assert c.passed is True
    assert c.severity == "warn"
    assert "1/8" in c.detail


def test_thrashing_run_warns(tmp_path):
    # 12 of 16 unproductive, spread across all three failure classes.
    classes = (["success"] * 4 + ["arg-invalid"] * 5
               + ["policy-refused"] * 4 + ["runtime-error"] * 3)
    report = ThirdUmpire.grade(_tx(tmp_path, classes))
    c = _thrash(report)
    assert c.passed is False
    assert c.severity == "warn"
    assert "12/16" in c.detail
    assert "75%" in c.detail
    # warn, never fail: the run may still have produced a correct artifact.
    assert report.verdict == "WARN"


def test_below_min_results_does_not_judge(tmp_path):
    # Under the floor the check reports informationally and never fails.
    classes = ["arg-invalid"] * (THRASH_MIN_RESULTS - 1)
    c = _thrash(ThirdUmpire.grade(_tx(tmp_path, classes)))
    assert c.passed is True
    assert c.severity == "info"
    assert f"{THRASH_MIN_RESULTS}-result floor" in c.detail


def test_at_min_results_does_judge(tmp_path):
    # Exactly at the floor, an all-unproductive run is judged.
    classes = ["arg-invalid"] * THRASH_MIN_RESULTS
    c = _thrash(ThirdUmpire.grade(_tx(tmp_path, classes)))
    assert c.passed is False
    assert c.severity == "warn"


def test_exact_threshold_fires(tmp_path):
    # The bar is >=, so an even split counts as thrashing.
    c = _thrash(ThirdUmpire.grade(_tx(tmp_path, ["success"] * 3 + ["runtime-error"] * 3)))
    assert c.passed is False
    assert "50%" in c.detail


def test_just_under_threshold_passes(tmp_path):
    # 3/7 = 0.428 — under the bar, so it passes even though failures are visible.
    c = _thrash(ThirdUmpire.grade(_tx(tmp_path, ["success"] * 4 + ["arg-invalid"] * 3)))
    assert c.passed is True


def test_unclassified_transcript_emits_no_check(tmp_path):
    # Pre-typed-feedback transcripts must read as "not measurable", not "clean".
    # A passing check here would silently vouch for a run nobody measured.
    report = ThirdUmpire.grade(_tx(tmp_path, ["success"] * 8, log_class=False))
    assert _thrash(report) is None
    assert report.summary["unproductive_ratio"] is None
    assert report.summary["results_by_class"] == {}


def test_summary_carries_class_tallies(tmp_path):
    report = ThirdUmpire.grade(_tx(tmp_path, ["success"] * 6 + ["policy-refused"] * 2))
    assert report.summary["results_by_class"] == {"success": 6, "policy-refused": 2}
    assert report.summary["unproductive_ratio"] == 0.25


def test_detail_names_the_dominant_class(tmp_path):
    # The operator needs to know WHICH way it failed to know what to fix.
    classes = ["success"] * 2 + ["policy-refused"] * 6
    c = _thrash(ThirdUmpire.grade(_tx(tmp_path, classes)))
    assert c.passed is False
    assert "policy-refused=6" in c.detail
    assert "envelope is mis-specified" in c.detail


def test_check_appears_in_exported_dict(tmp_path):
    # The verdict artifact is the consumable surface; the check must survive export.
    d = ThirdUmpire.grade(_tx(tmp_path, ["arg-invalid"] * 8)).as_dict()
    names = [c["name"] for c in d["checks"]]
    assert "thrashing" in names
    assert d["summary"]["unproductive_ratio"] == 1.0


def test_threshold_constants_are_sane():
    # Guards against an edit that silently disables the check.
    assert 0.0 < THRASH_RATIO <= 1.0
    assert THRASH_MIN_RESULTS >= 2


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


def test_end_to_end_real_run_is_gradeable(tmp_path):
    # The grading half is useless if a real run doesn't log result_class per
    # result. One good write + five unknown-tool calls = 5/6 unproductive.
    tpath = tmp_path / "run.jsonl"
    client = _ScriptClient(
        [("write_file", {"path": "out.md", "content": "x", "reason": "t"})]
        + [("frobnicate", {}) for _ in range(5)]
    )
    agent = Agent(name="a", system_prompt="x", workspace=str(tmp_path), client=client,
                  enable_fs=True, enable_shell=False, enable_web=False,
                  transcript=Transcript(path=tpath))
    env = Envelope(writable_paths=["out.md"], require_staging=False, repeat_halt=0)
    EnvelopeRunner(agent, env).run("go")

    c = _thrash(ThirdUmpire.grade(tpath))
    assert c is not None, "real run produced no gradeable result_class fields"
    assert c.passed is False
    assert "arg-invalid=5" in c.detail
