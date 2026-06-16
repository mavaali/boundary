"""Gated guarantees — present now, expected-fail until their enhancement lands.

Each is marked xfail (non-strict): it fails today (the enforcement doesn't exist)
and will flip to XPASS automatically when the named item ships — the signal to
remove the xfail marker and the check's expected_fail flag.
"""
from __future__ import annotations

import pytest

from boundary.selftest import (
    check_egress_blocked_empty_allowlist,
    check_denylist_bypass_blocked,
    check_taint_flow,
)


@pytest.mark.xfail(reason="needs Item 1 — OS-enforced egress (srt)", strict=False)
def test_egress_blocked_under_empty_allowlist():
    assert check_egress_blocked_empty_allowlist().passed


@pytest.mark.xfail(reason="needs Item 1/2 — egress proxy blocks denylist bypasses", strict=False)
def test_denylist_bypasses_are_blocked():
    assert check_denylist_bypass_blocked().passed


@pytest.mark.xfail(reason="needs Item 3 — taint/provenance dimension", strict=False)
def test_tainted_read_to_sink_triggers_taint_flow():
    assert check_taint_flow().passed
