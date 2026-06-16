"""Red-team fixture: a write outside the writable allowlist must be refused.

This asserts the SAME check that `boundary selftest` runs at runtime, so the
CLI guarantee and the CI assertion cannot drift apart.
"""
from __future__ import annotations

from boundary.selftest import check_write_outside_allowlist


def test_write_outside_allowlist_is_refused():
    result = check_write_outside_allowlist()
    assert result.passed, result.detail
