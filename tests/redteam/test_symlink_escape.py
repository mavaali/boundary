"""Red-team fixture: a symlink inside the workspace must not escape the jail.

The write case is the sharp one — the symlink's name is on the writable
allowlist, so only `Workspace.resolve()` following the link stands between the
agent and an out-of-jail write. This asserts the SAME check that
`boundary selftest` runs, so the CLI guarantee and the CI assertion can't drift.
"""
from __future__ import annotations

from boundary.selftest import check_symlink_escape_refused


def test_symlink_read_and_write_escapes_are_refused():
    result = check_symlink_escape_refused()
    assert result.passed, result.detail
