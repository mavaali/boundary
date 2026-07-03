"""Tests for cross-run spend budgets (boundary/budget.py + History.spend_since).

Budgets bound the SUM of run costs over calendar/rolling windows, aggregated
over the run-history `runs` table. Time is injected so window boundaries are
deterministic without freezing the clock.
"""
from __future__ import annotations

import datetime as _dt
import time

from boundary.budget import SpendBudget, _window_starts, evaluate_budget
from boundary.history import History

# --- parsing -------------------------------------------------------------

def test_from_config_empty_is_none():
    assert SpendBudget.from_config(None) is None
    assert SpendBudget.from_config({}) is None
    # A block with only non-window keys is still inactive.
    assert SpendBudget.from_config({"scope": "global"}) is None


def test_from_config_active():
    b = SpendBudget.from_config({"daily": 5, "monthly": 100, "scope": "global"})
    assert b is not None and b.is_active()
    assert b.daily == 5.0 and b.monthly == 100.0 and b.scope == "global"
    assert b.weekly is None


# --- window boundaries ---------------------------------------------------

def test_window_starts_align_to_calendar():
    # Friday 2026-07-03 15:30 local.
    now = _dt.datetime(2026, 7, 3, 15, 30, 0)
    starts = _window_starts(now)
    assert starts["daily"] == _dt.datetime(2026, 7, 3, 0, 0).timestamp()
    assert starts["weekly"] == _dt.datetime(2026, 6, 29, 0, 0).timestamp()  # Monday
    assert starts["monthly"] == _dt.datetime(2026, 7, 1, 0, 0).timestamp()


# --- aggregation with a fake source --------------------------------------

class _FakeSource:
    """spend_since over an in-memory list of (ts, ws, dollars, tags-dict)."""
    def __init__(self, rows):
        # Accept 3- or 4-tuples; missing tags default to {}.
        self.rows = [(r + ({},))[:4] for r in rows]

    def spend_since(self, workspace, since, tag=None):
        total = 0.0
        for ts, ws, d, tags in self.rows:
            if ts < since:
                continue
            if workspace is not None and ws != workspace:
                continue
            if tag is not None:
                key, val = tag
                if tags.get(key) != val:
                    continue
            total += d
        return total


def test_remaining_is_tightest_window():
    now = _dt.datetime(2026, 7, 3, 12, 0)
    today = _dt.datetime(2026, 7, 3, 9, 0).timestamp()
    src = _FakeSource([(today, "/ws", 0.90)])
    b = SpendBudget(daily=1.00, monthly=100.0)
    st = evaluate_budget(b, src, "/ws", now=now)
    assert not st.exhausted
    # Daily has $0.10 left, monthly $99.10 -> daily binds.
    assert st.binding == "daily"
    assert abs(st.remaining - 0.10) < 1e-9


def test_exhausted_when_window_over_cap():
    now = _dt.datetime(2026, 7, 3, 12, 0)
    today = _dt.datetime(2026, 7, 3, 9, 0).timestamp()
    src = _FakeSource([(today, "/ws", 1.20)])
    st = evaluate_budget(SpendBudget(daily=1.00), src, "/ws", now=now)
    assert st.exhausted
    assert st.remaining == 0.0  # clamped, never negative
    assert st.binding == "daily"


def test_scope_workspace_vs_global():
    now = _dt.datetime(2026, 7, 3, 12, 0)
    today = _dt.datetime(2026, 7, 3, 9, 0).timestamp()
    src = _FakeSource([(today, "/ws-a", 0.6), (today, "/ws-b", 0.6)])
    # Workspace scope sees only /ws-a's $0.60.
    ws_st = evaluate_budget(SpendBudget(daily=1.0, scope="workspace"), src, "/ws-a", now=now)
    assert abs(ws_st.remaining - 0.40) < 1e-9 and not ws_st.exhausted
    # Global scope sees both -> $1.20 > $1.00.
    g_st = evaluate_budget(SpendBudget(daily=1.0, scope="global"), src, "/ws-a", now=now)
    assert g_st.exhausted


def test_tag_scope_sums_per_tag_value():
    now = _dt.datetime(2026, 7, 3, 12, 0)
    today = _dt.datetime(2026, 7, 3, 9, 0).timestamp()
    src = _FakeSource([
        (today, "/ws-a", 0.7, {"tenant": "acme"}),
        (today, "/ws-b", 0.9, {"tenant": "acme"}),   # different workspace, same tenant
        (today, "/ws-a", 5.0, {"tenant": "globex"}),  # other tenant, ignored
    ])
    b = SpendBudget(daily=2.0, scope="tag", scope_tag="tenant")
    # This run is tenant=acme -> sums acme across workspaces = $1.60.
    st = evaluate_budget(b, src, "/ws-a", now=now, attribution={"tenant": "acme"})
    assert abs(st.windows["daily"]["spent"] - 1.60) < 1e-9
    assert not st.exhausted
    # A globex run sees only globex's $5.00 -> over the $2 cap.
    st2 = evaluate_budget(b, src, "/ws-a", now=now, attribution={"tenant": "globex"})
    assert st2.exhausted


def test_tag_shorthand_scope_parsing():
    b = SpendBudget.from_config({"monthly": 50, "scope": "tag:project"})
    assert b is not None
    assert b.scope == "tag" and b.scope_tag == "project"


def test_rolling_window_excludes_old_spend():
    now = _dt.datetime(2026, 7, 3, 12, 0)
    within = (now - _dt.timedelta(hours=2)).timestamp()
    stale = (now - _dt.timedelta(hours=30)).timestamp()
    src = _FakeSource([(within, "/ws", 0.5), (stale, "/ws", 5.0)])
    st = evaluate_budget(SpendBudget(rolling=1.0, rolling_hours=24.0), src, "/ws", now=now)
    # Only the $0.50 inside the trailing 24h counts.
    assert not st.exhausted
    assert abs(st.remaining - 0.5) < 1e-9


# --- real History integration --------------------------------------------

def _record(h, ws, dollars, started_at, attribution=None):
    h.record_run(
        schedule_name="s", persona="p", workspace=ws,
        started_at=started_at, ended_at=started_at + 1,
        stop_reason="stop", iterations=1, writes_executed=1,
        input_tokens=0, output_tokens=0, cached_input_tokens=0,
        estimated_dollars=dollars, wall_seconds=1.0,
        third_umpire_verdict="PASS", third_umpire_summary={},
        transcript_path=None, written_files=[], attribution=attribution,
    )


def test_history_spend_since_sums_by_workspace(tmp_path):
    h = History(db_path=tmp_path / "h.db")
    now = time.time()
    _record(h, "/ws", 0.30, now - 100)
    _record(h, "/ws", 0.20, now - 50)
    _record(h, "/other", 9.00, now - 50)
    assert abs(h.spend_since("/ws", now - 200) - 0.50) < 1e-9
    # Cutoff excludes the older run.
    assert abs(h.spend_since("/ws", now - 75) - 0.20) < 1e-9
    # Global sums across workspaces.
    assert abs(h.spend_since(None, now - 200) - 9.50) < 1e-9
    h.close()


def test_evaluate_over_real_history(tmp_path):
    h = History(db_path=tmp_path / "h.db")
    now = _dt.datetime(2026, 7, 3, 12, 0)
    at = _dt.datetime(2026, 7, 3, 10, 0).timestamp()
    _record(h, "/ws", 0.90, at)
    b = SpendBudget(daily=1.00)
    st = evaluate_budget(b, h, "/ws", now=now)
    assert not st.exhausted and st.binding == "daily"
    assert abs(st.remaining - 0.10) < 1e-9
    # One more run tips it over.
    _record(h, "/ws", 0.20, at)
    st2 = evaluate_budget(b, h, "/ws", now=now)
    assert st2.exhausted and st2.remaining == 0.0
    h.close()


def test_history_spend_since_filters_by_tag(tmp_path):
    h = History(db_path=tmp_path / "h.db")
    now = time.time()
    _record(h, "/ws-a", 0.30, now - 100, attribution={"tenant": "acme"})
    _record(h, "/ws-b", 0.40, now - 100, attribution={"tenant": "acme"})
    _record(h, "/ws-a", 9.00, now - 100, attribution={"tenant": "globex"})
    # Tag filter sums across workspaces for one tenant.
    assert abs(h.spend_since(None, now - 200, tag=("tenant", "acme")) - 0.70) < 1e-9
    assert abs(h.spend_since(None, now - 200, tag=("tenant", "globex")) - 9.00) < 1e-9
    # An unknown tag value matches nothing.
    assert h.spend_since(None, now - 200, tag=("tenant", "nope")) == 0.0
    h.close()


def test_legacy_db_migrates_attribution_column(tmp_path):
    # A DB written by a pre-attribution Boundary (current schema minus the
    # column) must gain attribution_json on open, without data loss.
    import sqlite3

    from boundary.history import SCHEMA
    old_schema = SCHEMA.replace(",\n    attribution_json TEXT", "")
    assert "attribution_json" not in old_schema
    db = tmp_path / "old.db"
    c = sqlite3.connect(str(db))
    c.executescript(old_schema)
    c.execute("INSERT INTO runs(started_at, estimated_dollars, workspace) VALUES (1.0, 0.5, '/ws')")
    c.commit()
    c.close()

    h = History(db_path=db)  # triggers the migration
    cols = {r[1] for r in h._conn.execute("PRAGMA table_info(runs)")}
    assert "attribution_json" in cols
    assert h.spend_since("/ws", 0.0) == 0.5                     # legacy row preserved
    assert h.spend_since("/ws", 0.0, tag=("tenant", "acme")) == 0.0
    h.close()


def test_tag_budget_over_real_history(tmp_path):
    h = History(db_path=tmp_path / "h.db")
    now = _dt.datetime(2026, 7, 3, 12, 0)
    at = _dt.datetime(2026, 7, 3, 10, 0).timestamp()
    _record(h, "/ws-a", 1.20, at, attribution={"tenant": "acme"})
    _record(h, "/ws-a", 5.00, at, attribution={"tenant": "globex"})
    b = SpendBudget(monthly=2.00, scope="tag", scope_tag="tenant")
    acme = evaluate_budget(b, h, "/ws-a", now=now, attribution={"tenant": "acme"})
    assert not acme.exhausted and abs(acme.remaining - 0.80) < 1e-9
    globex = evaluate_budget(b, h, "/ws-a", now=now, attribution={"tenant": "globex"})
    assert globex.exhausted
    h.close()
