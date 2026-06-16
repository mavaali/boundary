"""The selftest runner returns exit 0 while every enforced guarantee holds, and
non-zero if one regresses. Gated (expected_fail) checks never break the build.
"""
from __future__ import annotations

import subprocess
import sys

from boundary import selftest
from boundary.selftest import run_selftest, SelftestResult


def test_run_selftest_zero_when_guarantees_hold():
    assert run_selftest(verbose=False) == 0


def test_run_selftest_nonzero_when_an_enforced_guarantee_regresses(monkeypatch):
    def _regressed() -> SelftestResult:
        return SelftestResult("synthetic_regression", passed=False, detail="boom")

    monkeypatch.setattr(selftest, "CHECKS", selftest.CHECKS + [_regressed])
    assert run_selftest(verbose=False) != 0


def test_gated_failure_does_not_break_the_build(monkeypatch):
    def _gated_fail() -> SelftestResult:
        return SelftestResult("synthetic_gated", passed=False, detail="not yet", expected_fail=True)

    monkeypatch.setattr(selftest, "CHECKS", selftest.CHECKS + [_gated_fail])
    assert run_selftest(verbose=False) == 0


def test_cli_selftest_subcommand_exits_zero():
    proc = subprocess.run(
        [sys.executable, "-m", "boundary.cli", "selftest"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VERDICT: PASS" in proc.stdout
