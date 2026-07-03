"""Regression guards for the low-severity hardening from the security audit.

F8  — the fs write/read tools resolved a path and then re-opened it, a
      check-then-open TOCTOU: a symlink swapped in after resolve() would be
      followed out of the jail. Opens now use O_NOFOLLOW on the final component.
F9  — the Windows scheduler built an inline `cmd /c "..."` action with the
      schedule name / config path interpolated; metacharacters are now rejected.
F10 — the headless run-lock checked-then-wrote (non-atomic) and stole a lock on
      a bare PID match (PID reuse). It is now O_CREAT|O_EXCL atomic and validates
      PID start-time before stealing.
"""
from __future__ import annotations

import os

import pytest

from boundary.tools.workspace import Workspace

# --- F8: O_NOFOLLOW on the fs tools -------------------------------------------


def test_fs_roundtrip_still_works(tmp_path):
    from boundary.tools.fs import register_fs_tools
    from boundary.tools.registry import ToolRegistry

    ws = Workspace(tmp_path / "ws")
    reg = ToolRegistry()
    register_fs_tools(reg, ws)
    assert reg.get("write_file").fn(path="a.txt", content="hello\n", reason="x").startswith("wrote")
    assert reg.get("append_file").fn(path="a.txt", content="more", reason="x").startswith("appended")
    assert reg.get("edit_file").fn(path="a.txt", old_str="hello", new_str="HI", reason="x").startswith("edited")
    assert reg.get("read_file").fn(path="a.txt") == "HI\nmore"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is POSIX-only")
def test_secure_open_refuses_symlinked_final_component(tmp_path, monkeypatch):
    # Simulate the TOCTOU: resolve() validated a normal file, then the final
    # component was swapped to a symlink pointing outside before the open. We
    # model that by having resolve() return the (now-symlink) path; O_NOFOLLOW
    # must refuse it rather than follow the link out of the jail.
    ws = Workspace(tmp_path / "ws")
    outside = tmp_path / "secret"
    outside.write_text("SECRET")
    link = ws.root / "evil"
    link.symlink_to(outside)
    monkeypatch.setattr(ws, "resolve", lambda p: link)
    with pytest.raises(OSError):
        ws.secure_open("evil", "rb")
    with pytest.raises(OSError):
        ws.secure_open("evil", "w")
    assert outside.read_text() == "SECRET"  # never written through


# --- F9: Windows scheduler input validation -----------------------------------


def test_reject_unsafe_flags_metacharacters():
    from boundary.win_scheduler import _reject_unsafe
    _reject_unsafe("weekly report", "schedule name")  # spaces are fine
    _reject_unsafe("risk-review_2026", "schedule name")
    for bad in ['evil" & calc', "a|b", "x>y", "pct%path", "c^d", "back`tick"]:
        with pytest.raises(ValueError):
            _reject_unsafe(bad, "schedule name")


def test_install_common_rejects_unsafe_name(tmp_path, monkeypatch):
    import boundary.win_scheduler as w
    # Don't actually shell out to schtasks; the guard should fire first.
    monkeypatch.setattr(w, "_run_schtasks", lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached schtasks")))
    with pytest.raises(ValueError, match="unsafe"):
        w._install_common(tmp_path / "s.yaml", 'pwn" & calc', "daily@09:00", "schedule-run")


# --- F10: atomic, PID-reuse-safe run-lock -------------------------------------


def _fresh_lockdir(tmp_path, monkeypatch):
    import boundary.headless as h
    monkeypatch.setattr(h, "LOCK_DIR", tmp_path / "locks")
    return h


def test_lock_is_exclusive_for_live_holder(tmp_path, monkeypatch):
    h = _fresh_lockdir(tmp_path, monkeypatch)
    first = h._acquire_lock("sched")
    assert first is not None
    assert h._acquire_lock("sched") is None  # held by this live process
    h._release_lock(first)
    assert h._acquire_lock("sched") is not None  # released → acquirable again


def test_corrupt_and_dead_locks_are_stealable(tmp_path, monkeypatch):
    h = _fresh_lockdir(tmp_path, monkeypatch)
    (tmp_path / "locks").mkdir(parents=True)
    corrupt = tmp_path / "locks" / "c.lock"
    corrupt.write_text("not-a-pid")
    assert h._lock_holder_alive(corrupt) is False
    dead = tmp_path / "locks" / "d.lock"
    dead.write_text("999999\n\n")  # PID that (almost certainly) does not exist
    assert h._lock_holder_alive(dead) is False


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="PID start-time needs /proc")
def test_pid_reuse_is_detected(tmp_path, monkeypatch):
    h = _fresh_lockdir(tmp_path, monkeypatch)
    (tmp_path / "locks").mkdir(parents=True)
    lock = tmp_path / "locks" / "r.lock"
    # Our own live PID but a start-time that cannot match → treated as reuse.
    lock.write_text(f"{os.getpid()}\n0\n")
    assert h._lock_holder_alive(lock) is False
    # Correct start-time → recognized as alive.
    lock.write_text(f"{os.getpid()}\n{h._proc_start(os.getpid())}\n")
    assert h._lock_holder_alive(lock) is True
