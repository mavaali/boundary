"""CLI-level test for best-of-K wiring (hermetic — no network)."""
from __future__ import annotations

from boundary.cli import main


def test_runs_requires_envelope_writable(capsys):
    # --runs K must error before building any client when no writable path is set.
    rc = main(["run", "--task", "do x", "--runs", "3"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "requires --envelope-writable" in out
