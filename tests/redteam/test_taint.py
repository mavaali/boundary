"""Red-team fixture: tainted (untrusted external) content must not silently flow
into a writable sink (Item 3).

Enforced as of Item 3 — reading external content then writing trips the taint
gate; a workspace-only run does not (no false positive on the common case).
"""
from __future__ import annotations

from boundary.selftest import check_taint_flow


def test_tainted_read_to_sink_triggers_taint_flow():
    result = check_taint_flow()
    assert result.passed, result.detail
