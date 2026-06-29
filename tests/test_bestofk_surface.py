"""3b tests: schedule YAML best-of-K parsing + Fielding Coach dispatch guard."""
from __future__ import annotations

import pytest

from boundary.schedule import ScheduleConfig
from boundary.fielding_coach import EnvelopeProposal, dispatch_best_of_k


def test_schedule_parses_best_of_k_fields(tmp_path):
    y = tmp_path / "s.yaml"
    y.write_text(
        "name: nightly\n"
        "schedule: daily 09:00\n"
        "persona: writer\n"
        "workspace: /tmp/ws\n"
        "task: do the thing\n"
        "runs: 3\n"
        "select_margin: 0.2\n"
        "judge_model: claude-opus-4.7\n"
        "headless_fallback: defer\n"
        "envelope:\n"
        "  writable_paths: [out.md]\n"
    )
    cfg = ScheduleConfig.load(y)
    assert cfg.runs == 3
    assert cfg.select_margin == 0.2
    assert cfg.judge_model == "claude-opus-4.7"
    assert cfg.headless_fallback == "defer"


def test_schedule_defaults_single_run(tmp_path):
    y = tmp_path / "s.yaml"
    y.write_text(
        "name: n\nschedule: daily 09:00\npersona: w\nworkspace: /tmp/ws\ntask: t\n"
        "envelope:\n  writable_paths: [out.md]\n"
    )
    cfg = ScheduleConfig.load(y)
    assert cfg.runs == 1
    assert cfg.headless_fallback == "auto_pick_flag"


def test_dispatch_best_of_k_missing_charter_raises(tmp_path):
    # Charter existence is checked before any client is built — hermetic.
    proposal = EnvelopeProposal(
        restated_intent="x", persona="ghost", writable_paths=["out.md"],
        max_writes=1, min_writes=1, max_iters=10, task="t", rationale="r",
    )
    with pytest.raises(FileNotFoundError):
        dispatch_best_of_k(proposal, workspace=tmp_path, runs=3)
