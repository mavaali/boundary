"""Red-team fixture: a commit-class tool under on_commit='refuse' must not run.

The check uses a commit tool that records its side effect; the guarantee is that
the side effect never happens — the irreversible action is blocked, not merely
logged.
"""
from __future__ import annotations

from boundary.selftest import check_commit_refused


def test_commit_tool_under_refuse_is_not_executed():
    result = check_commit_refused()
    assert result.passed, result.detail
