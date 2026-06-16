"""Gated guarantees — present now, expected-fail until their enhancement lands.

Marked xfail (non-strict): fails today (the enforcement doesn't exist) and flips
to XPASS automatically when the named item ships — the signal to remove the
marker and the check's expected_fail flag.

(Egress + denylist-bypass moved to tests/redteam/test_egress.py once the srt
driver landed — they are enforced where srt is available.)
"""
from __future__ import annotations

import pytest

from boundary.selftest import check_taint_flow


@pytest.mark.xfail(reason="needs Item 3 — taint/provenance dimension", strict=False)
def test_tainted_read_to_sink_triggers_taint_flow():
    assert check_taint_flow().passed
