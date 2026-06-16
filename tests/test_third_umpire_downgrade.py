"""Item 6 — the Third Umpire surfaces envelope downgrades.

A run that disabled a guardrail (staging gate off, on_commit=allow, on_taint=allow)
must be visibly distinct from one that never needed it: the verdict carries an
`envelope_downgrade` line naming each disabled gate. A normal run produces no
such line.
"""
from __future__ import annotations

import json
from pathlib import Path

from boundary.third_umpire import ThirdUmpire, downgrade_tags


def test_downgrade_tags_names_each_disabled_gate():
    assert downgrade_tags(require_staging=False, on_commit="allow") == [
        "staging_gate=off",
        "on_commit=allow",
    ]


def test_downgrade_tags_empty_for_clean_run():
    assert downgrade_tags(require_staging=True, on_commit="refuse") == []


def test_downgrade_tags_ignores_unknown_require_staging():
    # None (e.g. a transcript with no envelope_start) must not be mis-flagged.
    assert downgrade_tags(require_staging=None, on_commit="refuse") == []


def test_downgrade_tags_includes_on_taint_when_allow():
    assert downgrade_tags(require_staging=True, on_commit="refuse", on_taint="allow") == [
        "on_taint=allow",
    ]


def _write_transcript(tmp_path: Path, *, require_staging: bool, on_commit: str) -> Path:
    events = [
        {"type": "envelope_start", "require_staging": require_staging, "writable_paths": ["out.md"]},
        {"type": "envelope_end", "events": [], "on_commit": on_commit, "commit_allowlist": []},
        {"type": "end", "iterations": 1},
    ]
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return path


def _downgrade_checks(report):
    return [c for c in report.checks if c.name == "envelope_downgrade"]


def test_downgrade_surfaced_when_staging_off_and_commit_allow(tmp_path):
    report = ThirdUmpire.grade(_write_transcript(tmp_path, require_staging=False, on_commit="allow"))
    dc = _downgrade_checks(report)
    assert len(dc) == 1, "expected exactly one envelope_downgrade check"
    assert not dc[0].passed and dc[0].severity == "warn"
    assert "staging_gate=off" in dc[0].detail
    assert "on_commit=allow" in dc[0].detail


def test_no_downgrade_line_on_a_normal_run(tmp_path):
    report = ThirdUmpire.grade(_write_transcript(tmp_path, require_staging=True, on_commit="refuse"))
    assert _downgrade_checks(report) == []


def test_only_staging_downgrade_named_when_commit_is_safe(tmp_path):
    report = ThirdUmpire.grade(_write_transcript(tmp_path, require_staging=False, on_commit="refuse"))
    dc = _downgrade_checks(report)
    assert len(dc) == 1
    assert "staging_gate=off" in dc[0].detail
    assert "on_commit" not in dc[0].detail
