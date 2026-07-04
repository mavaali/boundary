"""Cost attribution on the interactive `boundary run` path.

Covers the --attribution flag parsing, the malformed-flag CLI exit, and the
adhoc-run recording helper that writes an interactive envelope run to the shared
history ledger with its tags.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boundary.cli import _parse_attribution, _record_adhoc_run, main
from boundary.history import History

# --- flag parsing --------------------------------------------------------

def test_parse_attribution_pairs():
    assert _parse_attribution([]) == {}
    assert _parse_attribution(["tenant=acme", "project=pricing"]) == {
        "tenant": "acme", "project": "pricing"}
    # Whitespace trimmed; value may contain '='.
    assert _parse_attribution([" tenant = acme ", "expr=a=b"]) == {
        "tenant": "acme", "expr": "a=b"}


def test_parse_attribution_rejects_malformed():
    with pytest.raises(ValueError):
        _parse_attribution(["notakeyvalue"])
    with pytest.raises(ValueError):
        _parse_attribution(["=novalue"])


def test_run_malformed_attribution_exits_2(capsys):
    # Malformed --attribution is caught before any client/agent is built.
    rc = main(["run", "--task", "x", "--attribution", "bad", "--envelope-writable", "out.md"])
    assert rc == 2
    assert "KEY=VALUE" in capsys.readouterr().out


# --- adhoc recording helper ---------------------------------------------

def _fake_result(dollars=0.42):
    return SimpleNamespace(
        loop_result=SimpleNamespace(stop_reason="stop", iterations=3),
        writes_executed=1, input_tokens=1000, output_tokens=200,
        cached_input_tokens=50, estimated_dollars=dollars, wall_seconds=2.5,
    )


def test_record_adhoc_run_writes_ledger_with_tags(tmp_path):
    db = tmp_path / "h.db"
    run_id, err = _record_adhoc_run(
        _fake_result(0.42), workspace="/ws", persona=None,
        attribution={"tenant": "acme"}, started_at=100.0, ended_at=103.0,
        transcript_path=None, db_path=db,
    )
    assert err is None and run_id is not None

    h = History(db_path=db)
    # Recorded as an adhoc row (schedule_name is NULL) with the spend.
    rows = h.list_runs(limit=5)
    assert len(rows) == 1
    assert rows[0]["schedule_name"] is None
    assert abs(rows[0]["estimated_dollars"] - 0.42) < 1e-9
    # And it is attributable: the tag filter finds its spend.
    assert abs(h.spend_since("/ws", 0.0, tag=("tenant", "acme")) - 0.42) < 1e-9
    assert h.spend_since("/ws", 0.0, tag=("tenant", "globex")) == 0.0
    h.close()


def test_record_adhoc_run_reports_error_without_raising(tmp_path):
    # A db path whose parent is a FILE (not a dir) can't be created -> the helper
    # must return an error string, never raise (ledger write is best-effort).
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    run_id, err = _record_adhoc_run(
        _fake_result(), workspace="/ws", persona=None, attribution={},
        started_at=1.0, ended_at=2.0, transcript_path=None,
        db_path=blocker / "h.db",   # parent 'afile' is a regular file
    )
    assert run_id is None
    assert err is not None
