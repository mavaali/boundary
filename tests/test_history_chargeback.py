"""Chargeback rollup: total agent spend grouped by an attribution tag.

Attribution stamps every run with str→str tags (tenant, project, …). Budgets can
already be *scoped* per tag value; this is the read side — "here is what each
tenant's agent runs cost this month," the bill an operator would hand a client.
"""
from __future__ import annotations

import pytest

from boundary.history import History


def _rec(h, *, tenant, dollars, started_at=100.0):
    h.record_run(
        schedule_name=None, persona=None, workspace="/ws",
        started_at=started_at, ended_at=started_at + 1.0, stop_reason="stop",
        iterations=1, writes_executed=1, input_tokens=100, output_tokens=50,
        cached_input_tokens=0, estimated_dollars=dollars, wall_seconds=1.0,
        third_umpire_verdict="PASS", third_umpire_summary={}, transcript_path=None,
        written_files=[], attribution=({"tenant": tenant} if tenant else {}),
    )


def test_spend_by_tag_groups_and_sums(tmp_path):
    h = History(db_path=tmp_path / "h.db")
    _rec(h, tenant="acme", dollars=0.40)
    _rec(h, tenant="acme", dollars=0.10)
    _rec(h, tenant="globex", dollars=0.25)
    _rec(h, tenant=None, dollars=0.05)   # untagged -> None bucket
    roll = h.spend_by_tag("tenant")
    assert roll["acme"]["cost"] == pytest.approx(0.50)
    assert roll["acme"]["runs"] == 2
    assert roll["globex"]["cost"] == pytest.approx(0.25)
    assert roll[None]["cost"] == pytest.approx(0.05)
    h.close()


def test_spend_by_tag_windows_by_since(tmp_path):
    h = History(db_path=tmp_path / "h.db")
    _rec(h, tenant="acme", dollars=1.0, started_at=100.0)
    _rec(h, tenant="acme", dollars=2.0, started_at=200.0)
    roll = h.spend_by_tag("tenant", since=150.0)
    assert roll["acme"]["cost"] == pytest.approx(2.0)
    assert roll["acme"]["runs"] == 1
    h.close()


def test_spend_by_tag_rejects_unsafe_key(tmp_path):
    h = History(db_path=tmp_path / "h.db")
    with pytest.raises(ValueError):
        h.spend_by_tag("bad key!")
    h.close()


def test_cli_history_by_tag_prints_rollup(tmp_path, monkeypatch, capsys):
    import boundary.history as hm
    db = tmp_path / "h.db"
    real = hm.History
    seed = real(db_path=db)
    _rec(seed, tenant="acme", dollars=0.40)
    _rec(seed, tenant="acme", dollars=0.10)
    _rec(seed, tenant="globex", dollars=0.25)
    seed.close()
    # The CLI constructs History() with the default DB; redirect it to our temp DB.
    monkeypatch.setattr(hm, "History", lambda *a, **k: real(db_path=db))

    from boundary.cli import main
    rc = main(["history", "--by", "tenant"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tenant" in out
    assert "acme" in out and "0.50" in out    # acme's two runs summed
    assert "globex" in out and "0.25" in out
    assert "total" in out.lower()
