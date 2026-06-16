"""Red-team fixture: a write attempted before `stage_proposal` must be refused.

The staging pivot forces the agent to commit a provisional thesis before it can
write. Skipping it and going straight to a write is a guarantee violation.
"""
from __future__ import annotations

from boundary.selftest import check_staging_gate_before_write


def test_write_before_staging_is_refused():
    result = check_staging_gate_before_write()
    assert result.passed, result.detail
