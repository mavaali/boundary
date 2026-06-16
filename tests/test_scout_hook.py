from __future__ import annotations

import json

from boundary import headless
from boundary.schedule import ScheduleConfig


def _config(tmp_path, notify):
    return ScheduleConfig(
        name="wiki-health",
        schedule="daily 09:00",
        persona="fury",
        workspace=str(tmp_path),
        task="audit",
        writable_paths=["scratch/wiki-health-{date}.md"],
        notify=notify,
    )


def test_scout_hook_event_written_for_warn(tmp_path, monkeypatch):
    event_dir = tmp_path / "events" / "pending"
    monkeypatch.setattr(headless, "EVENT_PENDING_DIR", event_dir)
    (tmp_path / "scratch").mkdir()

    path = headless._emit_scout_hook_event(
        _config(tmp_path, {
            "scout_hook": {
                "on": "warn_fail",
                "channel": "teams_dm",
                "summary_file": "scratch/wiki-health-{date}.md",
            },
        }),
        run_id=42,
        review_id=None,
        stop_reason="stop",
        third_umpire_verdict="WARN",
        transcript_path="/tmp/transcript.jsonl",
        written_files=[str(tmp_path / "scratch" / "wiki-health-2026-06-16.md")],
        error_text=None,
        rendered_paths=["scratch/wiki-health-2026-06-16.md"],
        wall_seconds=12.5,
        estimated_dollars=0.03,
    )

    assert path is not None
    event = json.loads((event_dir / "wiki-health-42.json").read_text())
    assert event["type"] == "boundary.schedule.completed"
    assert event["schedule"] == "wiki-health"
    assert event["third_umpire_verdict"] == "WARN"
    assert event["channel"] == "teams_dm"
    summary = event["summary_file"].replace("\\", "/")
    assert summary.endswith("/scratch/wiki-health-2026-06-16.md")


def test_scout_hook_not_written_for_pass_when_warn_fail(tmp_path, monkeypatch):
    event_dir = tmp_path / "events" / "pending"
    monkeypatch.setattr(headless, "EVENT_PENDING_DIR", event_dir)

    path = headless._emit_scout_hook_event(
        _config(tmp_path, {"scout_hook": {"on": "warn_fail"}}),
        run_id=7,
        review_id=None,
        stop_reason="stop",
        third_umpire_verdict="PASS",
        transcript_path=None,
        written_files=[],
        error_text=None,
        rendered_paths=[],
        wall_seconds=1.0,
        estimated_dollars=0.0,
    )

    assert path is None
    assert not event_dir.exists()
