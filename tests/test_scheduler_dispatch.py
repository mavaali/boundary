"""Unit tests for the platform dispatcher.

The actual macOS / Windows backends are tested elsewhere (launchd via the
existing schedule integration tests; schtasks via tests/test_win_scheduler.py).
This file just validates that the dispatcher binds to the right symbols and
that the unsupported-platform fallback raises clearly.
"""
from __future__ import annotations

import importlib
import sys

import pytest


def _reload_scheduler():
    if "boundary.scheduler" in sys.modules:
        del sys.modules["boundary.scheduler"]
    return importlib.import_module("boundary.scheduler")


def test_dispatcher_picks_launchd_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    sched = _reload_scheduler()
    assert sched.BACKEND == "launchd"
    # Sanity: the launchd functions are re-exported.
    from boundary import launchd
    assert sched.install is launchd.install
    assert sched.uninstall is launchd.uninstall
    assert sched.install_pipeline is launchd.install_pipeline


def test_dispatcher_picks_schtasks_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    sched = _reload_scheduler()
    assert sched.BACKEND == "schtasks"
    from boundary import win_scheduler
    assert sched.install is win_scheduler.install
    assert sched.uninstall is win_scheduler.uninstall
    assert sched.install_pipeline is win_scheduler.install_pipeline


def test_dispatcher_unsupported_on_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    sched = _reload_scheduler()
    assert sched.BACKEND == "unsupported"
    assert sched.list_installed() == []
    with pytest.raises(RuntimeError, match="only supported on macOS"):
        sched.install("/tmp/whatever.yaml")
    with pytest.raises(RuntimeError, match="only supported on macOS"):
        sched.install_pipeline("/tmp/whatever.yaml")
    with pytest.raises(RuntimeError, match="only supported on macOS"):
        sched.uninstall("foo")
