"""Unit tests for the Windows headless scheduler backend.

Runs on every platform — schtasks calls are mocked. Covers schedule mapping,
action wrapping, install + uninstall flow, and list_installed enumeration.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from boundary import win_scheduler as ws


def _ok():
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fail(stderr="boom"):
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def test_label_for_handles_spaces_and_slashes():
    assert ws.label_for("my schedule/foo") == "io.boundary.schedule.my_schedule_foo"


def test_schtasks_args_interval():
    args = ws._schtasks_args_for_schedule("every 30 minutes")
    assert args == ["/sc", "MINUTE", "/mo", "30"]


def test_schtasks_args_hourly():
    args = ws._schtasks_args_for_schedule("hourly")
    assert args == ["/sc", "MINUTE", "/mo", "60"]


def test_schtasks_args_daily():
    args = ws._schtasks_args_for_schedule("daily 09:05")
    assert args == ["/sc", "DAILY", "/st", "09:05"]


def test_schtasks_args_weekly():
    args = ws._schtasks_args_for_schedule("weekly mon 14:00")
    assert args == ["/sc", "WEEKLY", "/d", "MON", "/st", "14:00"]


def test_schtasks_args_cron_rejected():
    with pytest.raises(ValueError, match="raw cron not supported"):
        ws._schtasks_args_for_schedule("cron:0 9 * * 1")


def test_build_action_wraps_with_cmd_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "SCHEDULER_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(ws, "_boundary_bin", lambda: "C:\\bin\\boundary.exe")
    cfg_path = tmp_path / "sched.yaml"
    cfg_path.write_text("dummy")
    action = ws._build_action(cfg_path, "schedule-run", "io.boundary.schedule.foo")
    assert action.startswith('cmd /c "')
    assert '"C:\\bin\\boundary.exe"' in action
    assert "schedule-run" in action
    assert ">>" in action and "2>>" in action
    assert "io.boundary.schedule.foo.out.log" in action
    assert "io.boundary.schedule.foo.err.log" in action


def test_install_common_calls_schtasks_create_and_writes_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "TASK_LIST_DIR", tmp_path / "tasks")
    monkeypatch.setattr(ws, "SCHEDULER_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(ws, "_boundary_bin", lambda: "boundary.exe")

    calls: list[list[str]] = []

    def fake_run(args, **_):
        calls.append(args)
        return _ok()

    monkeypatch.setattr(subprocess, "run", fake_run)

    cfg_path = tmp_path / "sched.yaml"
    cfg_path.write_text("dummy")

    marker = ws._install_common(cfg_path, "demo", "daily 09:00", "schedule-run")

    # 1st call: /delete (idempotent), 2nd: /create.
    assert calls[0][0] == "schtasks"
    assert "/delete" in calls[0]
    assert "/create" in calls[1]
    assert "/sc" in calls[1] and "DAILY" in calls[1]
    assert marker.exists()
    contents = marker.read_text()
    assert "io.boundary.schedule.demo" in contents
    assert "schedule-run" in contents


def test_install_common_raises_on_create_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "TASK_LIST_DIR", tmp_path / "tasks")
    monkeypatch.setattr(ws, "SCHEDULER_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(ws, "_boundary_bin", lambda: "boundary.exe")

    seq = iter([_ok(), _fail("schtasks: bad")])

    def fake_run(args, **_):
        return next(seq)

    monkeypatch.setattr(subprocess, "run", fake_run)

    cfg_path = tmp_path / "sched.yaml"
    cfg_path.write_text("dummy")

    with pytest.raises(RuntimeError, match="schtasks /create failed"):
        ws._install_common(cfg_path, "demo", "daily 09:00", "schedule-run")
    # marker must NOT have been written on failure.
    assert not (tmp_path / "tasks" / "io.boundary.schedule.demo.task").exists()


def test_uninstall_removes_marker_and_calls_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "TASK_LIST_DIR", tmp_path / "tasks")
    (tmp_path / "tasks").mkdir()
    marker = tmp_path / "tasks" / "io.boundary.schedule.demo.task"
    marker.write_text("\\boundary\\io.boundary.schedule.demo\nfoo\nschedule-run\ndaily 09:00\n")

    calls: list[list[str]] = []

    def fake_run(args, **_):
        calls.append(args)
        return _ok()

    monkeypatch.setattr(subprocess, "run", fake_run)

    ws.uninstall("demo")
    assert any("/delete" in c for c in calls)
    assert not marker.exists()


def test_list_installed_enumerates_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "TASK_LIST_DIR", tmp_path / "tasks")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "io.boundary.schedule.a.task").write_text("x")
    (tmp_path / "tasks" / "io.boundary.schedule.b.task").write_text("x")
    (tmp_path / "tasks" / "unrelated.txt").write_text("x")
    out = ws.list_installed()
    names = sorted(p.name for p in out)
    assert names == ["io.boundary.schedule.a.task", "io.boundary.schedule.b.task"]


def test_list_installed_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "TASK_LIST_DIR", tmp_path / "missing")
    assert ws.list_installed() == []
