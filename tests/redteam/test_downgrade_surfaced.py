"""Red-team fixture: a disabled guardrail must be surfaced by the Third Umpire.

Enforced as of Item 6 — a run with the staging gate off (or on_commit=allow)
produces an `envelope_downgrade` line, so a downgraded run can't masquerade as a
clean one.
"""
from __future__ import annotations

from boundary.selftest import check_downgrade_surfaced


def test_envelope_downgrade_is_surfaced():
    result = check_downgrade_surfaced()
    assert result.passed, result.detail
