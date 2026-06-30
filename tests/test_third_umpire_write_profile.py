from __future__ import annotations

import json

from boundary.third_umpire import ThirdUmpire


def _tx(tmp_path, *, write_profile, in_tok, out_tok, writes=1):
    path = tmp_path / "t.jsonl"
    records = [
        {"type": "envelope_start", "writable_paths": ["out.md"],
         "require_staging": True, "write_profile": write_profile},
        {"type": "assistant", "iteration": 1, "content": "[DATA] done", "tool_calls": []},
        {"type": "envelope_end", "writes_attempted": writes, "writes_executed": writes,
         "external_calls": 0, "commit_attempted": 0, "commit_executed": 0,
         "staged": True, "unstaged_reads": 1, "stage_calls": 1,
         "input_tokens": in_tok, "output_tokens": out_tok, "estimated_dollars": 0.45,
         "events": [{"kind": "staged", "tool": "stage_proposal", "detail": "", "iteration": 1}]},
        {"type": "end", "iterations": 2},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _spend(report):
    return next(c for c in report.checks if c.name == "spend_pacing")


def test_synthesis_high_input_passes(tmp_path):
    # The real test-fire shape: ~494K in, ~5K out, 1 write -> previously FAIL.
    r = ThirdUmpire.grade(_tx(tmp_path, write_profile="synthesis", in_tok=494_118, out_tok=5_369))
    c = _spend(r)
    assert c.passed is True and c.severity == "warn"
    assert "synthesis" in c.detail
    # The decisive proof: spend_pacing no longer contributes a FAIL for this shape.
    assert not (c.severity == "fail")


def test_synthesis_churn_fails(tmp_path):
    # Read a ton, wrote nothing meaningful -> the real synthesis failure mode.
    c = _spend(ThirdUmpire.grade(_tx(tmp_path, write_profile="synthesis", in_tok=400_000, out_tok=120)))
    assert c.passed is False and c.severity == "fail"
    assert "churn" in c.detail


def test_synthesis_runaway_fails(tmp_path):
    c = _spend(ThirdUmpire.grade(_tx(tmp_path, write_profile="synthesis", in_tok=2_000_000, out_tok=5_000)))
    assert c.passed is False and c.severity == "fail"


def test_edit_profile_unchanged(tmp_path):
    # edit profile keeps the old tokens/write axis: 494K -> FAIL.
    c = _spend(ThirdUmpire.grade(_tx(tmp_path, write_profile="edit", in_tok=494_118, out_tok=5_369)))
    assert c.passed is False and c.severity == "fail"
    assert "[edit]" in c.detail


def test_default_profile_is_edit(tmp_path):
    # transcript with no write_profile -> behaves as edit (backward compatible).
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"type": "envelope_start", "writable_paths": ["out.md"]},
        {"type": "assistant", "iteration": 1, "content": "x", "tool_calls": []},
        {"type": "envelope_end", "writes_executed": 1, "input_tokens": 350_000,
         "output_tokens": 1000, "estimated_dollars": 0.1, "events": []},
        {"type": "end", "iterations": 2},
    ]) + "\n")
    c = _spend(ThirdUmpire.grade(path))
    assert c.severity == "fail" and "[edit]" in c.detail
