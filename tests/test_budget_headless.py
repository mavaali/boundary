"""Integration: run_headless honours the cross-run spend budget.

The budget gate runs BEFORE the charter/agent is loaded, so the skip path can
be exercised without a real persona — an exhausted window returns
stop_reason="skipped_budget" and incurs no spend.
"""
from __future__ import annotations

import time

from boundary import headless
from boundary.budget import SpendBudget
from boundary.history import History
from boundary.schedule import ScheduleConfig


def _record(h, ws, dollars):
    h.record_run(
        schedule_name="seed", persona="p", workspace=ws,
        started_at=time.time(), ended_at=time.time() + 1,
        stop_reason="stop", iterations=1, writes_executed=1,
        input_tokens=0, output_tokens=0, cached_input_tokens=0,
        estimated_dollars=dollars, wall_seconds=1.0,
        third_umpire_verdict="PASS", third_umpire_summary={},
        transcript_path=None, written_files=[],
    )


def test_exhausted_budget_skips_run(tmp_path, monkeypatch):
    # Isolate the run-lock dir so the test doesn't touch ~/.boundary.
    monkeypatch.setattr(headless, "LOCK_DIR", tmp_path / "locks")
    db = tmp_path / "h.db"
    ws = str(tmp_path / "ws")

    # Pre-seed the ledger so today's daily window is already blown.
    h = History(db_path=db)
    _record(h, ws, 2.00)
    h.close()

    config = ScheduleConfig(
        name="budget-skip-test", schedule="manual", persona="none",
        workspace=ws, task="do a thing",
        spend_budget=SpendBudget(daily=1.00),
    )
    out = headless.run_headless(config, db_path=db)

    assert out["stop_reason"] == "skipped_budget"
    assert out["dollars"] == 0.0
    assert out["run_id"] is None
    assert out["budget"]["exhausted"] is True
    assert out["budget"]["binding"] == "daily"
    assert "daily" in out["error"]


def test_budget_with_headroom_does_not_skip(tmp_path, monkeypatch):
    # With headroom the gate must NOT skip — it falls through to the normal run
    # path, which here fails on the missing persona charter (expected). The point
    # is that the stop_reason is an ordinary error, not "skipped_budget".
    monkeypatch.setattr(headless, "LOCK_DIR", tmp_path / "locks")
    db = tmp_path / "h.db"
    ws = str(tmp_path / "ws")

    h = History(db_path=db)
    _record(h, ws, 0.10)  # only $0.10 of a $1.00 daily budget spent
    h.close()

    config = ScheduleConfig(
        name="budget-headroom-test", schedule="manual", persona="none",
        workspace=ws, task="do a thing",
        spend_budget=SpendBudget(daily=1.00),
    )
    out = headless.run_headless(config, db_path=db)
    assert out["stop_reason"] != "skipped_budget"
