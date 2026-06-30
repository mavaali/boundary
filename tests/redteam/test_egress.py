"""Red-team fixtures: OS-enforced network egress (Item 1, srt driver).

Skipped where srt is unavailable; enforced where it is. Egress is asserted
against a loopback sink so the result is deterministic and needs no real network.
"""
from __future__ import annotations

import shutil

import pytest

from boundary.selftest import check_denylist_bypass_blocked, check_egress_blocked_empty_allowlist

SRT_AVAILABLE = shutil.which("srt") is not None
pytestmark = pytest.mark.skipif(not SRT_AVAILABLE, reason="srt required")


def test_egress_blocked_under_empty_allowlist():
    result = check_egress_blocked_empty_allowlist()
    assert result.passed, result.detail


def test_denylist_bypass_blocked_by_proxy_not_denylist():
    result = check_denylist_bypass_blocked()
    assert result.passed, result.detail
