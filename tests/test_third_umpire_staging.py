from __future__ import annotations
import json

from boundary.third_umpire import ThirdUmpire


def _write_transcript(tmp_path, envelope_events):
    path = tmp_path / "transcript.jsonl"
    records = [
        {
            "type": "envelope_start",
            "writable_paths": ["out.md"],
            "require_staging": True,
        },
        {
            "type": "assistant",
            "iteration": 1,
            "content": "[DATA] Done.",
            "tool_calls": [],
        },
        {
            "type": "envelope_end",
            "writes_attempted": 1,
            "writes_executed": 1,
            "external_calls": 0,
            "commit_attempted": 0,
            "commit_executed": 0,
            "staged": any(e["kind"] == "staged" for e in envelope_events),
            "unstaged_reads": 1,
            "stage_calls": sum(1 for e in envelope_events if e["kind"] == "staged"),
            "events": envelope_events,
        },
        {
            "type": "end",
            "iterations": 2,
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_third_umpire_passes_staged_before_write(tmp_path):
    transcript = _write_transcript(tmp_path, [
        {"kind": "staged", "tool": "stage_proposal", "detail": "thesis=x", "iteration": 2},
        {"kind": "write_allowed", "tool": "write_file", "detail": "path=out.md", "iteration": 3},
    ])

    report = ThirdUmpire.grade(transcript)

    staging = _check(report, "staging_pivot")
    assert staging.passed is True
    assert "staged at iter 2" in staging.detail


def test_third_umpire_fails_missing_staging(tmp_path):
    transcript = _write_transcript(tmp_path, [
        {"kind": "write_allowed", "tool": "write_file", "detail": "path=out.md", "iteration": 3},
    ])

    report = ThirdUmpire.grade(transcript)

    staging = _check(report, "staging_pivot")
    assert staging.passed is False
    assert staging.severity == "fail"
    assert "no stage_proposal" in staging.detail


def test_third_umpire_fails_write_before_staging(tmp_path):
    transcript = _write_transcript(tmp_path, [
        {"kind": "write_allowed", "tool": "write_file", "detail": "path=out.md", "iteration": 2},
        {"kind": "staged", "tool": "stage_proposal", "detail": "thesis=x", "iteration": 3},
    ])

    report = ThirdUmpire.grade(transcript)

    staging = _check(report, "staging_pivot")
    assert staging.passed is False
    assert staging.severity == "fail"
    assert "write at iter 2 before staging" in staging.detail
