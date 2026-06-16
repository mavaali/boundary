"""Pluggable OS sandbox drivers for the workspace bash tool.

Boundary spawns `bash`, which itself spawns child processes (curl, git, ...).
A driver must enforce the workspace write-jail — and, for `srt`, a network
egress allowlist — across that whole process tree.

Drivers:
- ``seatbelt`` — macOS ``sandbox-exec`` with a Seatbelt profile (write-jail only;
  network egress is NOT bounded). The historical default.
- ``srt`` — Anthropic's sandbox-runtime: Seatbelt (macOS) / bubblewrap (Linux) +
  a proxy-enforced egress allowlist over the entire process tree.
- ``none`` — no OS sandbox. Explicit, loud opt-out.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

SANDBOX_DRIVERS = ("seatbelt", "srt", "none")


def _jail_env(workspace_root: Path) -> dict:
    """Env that points caches/temp/HOME at the workspace so stray writes land
    inside the jail rather than the real home directory."""
    tmp_dir = workspace_root / ".boundary-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "HOME": str(workspace_root),
        "TMPDIR": str(tmp_dir),
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "XDG_CACHE_HOME": str(tmp_dir / "cache"),
        "XDG_CONFIG_HOME": str(tmp_dir / "config"),
        "XDG_DATA_HOME": str(tmp_dir / "data"),
    })
    return env


def _format(r: subprocess.CompletedProcess) -> str:
    out = (r.stdout or "") + (r.stderr or "")
    return f"[exit {r.returncode}]\n{out[-8000:]}"


def run_sandboxed(
    command: str,
    *,
    workspace_root: Path,
    timeout: int,
    driver: str = "seatbelt",
    egress_allowlist: list[str] | None = None,
) -> str:
    root = Path(workspace_root).resolve()
    if driver == "seatbelt":
        return _run_seatbelt(command, root, timeout)
    if driver == "srt":
        return _run_srt(command, root, timeout, egress_allowlist or [])
    if driver == "none":
        return _run_none(command, root, timeout)
    return f"ERROR: unknown sandbox driver {driver!r} (expected one of {SANDBOX_DRIVERS})."


# ---- seatbelt (macOS) -------------------------------------------------------

def _sandbox_literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sandbox_profile(workspace_root: Path) -> str:
    root = workspace_root.resolve()
    return "\n".join([
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* (subpath {_sandbox_literal(str(root))}))",
        "",
    ])


def _run_seatbelt(command: str, root: Path, timeout: int) -> str:
    if platform.system() != "Darwin":
        return "ERROR: the seatbelt driver is macOS-only (sandbox-exec unavailable). Use --sandbox-driver srt on Linux."
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return "ERROR: sandbox-exec not found; refusing to run unsandboxed bash."
    env = _jail_env(root)
    profile = _sandbox_profile(root)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".sb", delete=False) as f:
        f.write(profile)
        profile_path = f.name
    try:
        r = subprocess.run(
            [sandbox_exec, "-f", profile_path, "/bin/bash", "-lc", command],
            cwd=str(root), env=env, capture_output=True, text=True, timeout=timeout,
        )
        return _format(r)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"
    finally:
        try:
            Path(profile_path).unlink()
        except OSError:
            pass


# ---- srt (cross-platform + egress allowlist) --------------------------------

def _srt_settings(root: Path, egress_allowlist: list[str]) -> dict:
    return {
        "network": {"allowedDomains": list(egress_allowlist), "deniedDomains": []},
        "filesystem": {
            "allowRead": ["/"],
            "allowWrite": [str(root)],
            "denyRead": [],
            "denyWrite": [],
        },
    }


def _run_srt(command: str, root: Path, timeout: int, egress_allowlist: list[str]) -> str:
    srt = shutil.which("srt")
    if not srt:
        return (
            "ERROR: srt not found. Install with `npm install -g @anthropic-ai/sandbox-runtime` "
            "or choose a different --sandbox-driver."
        )
    env = _jail_env(root)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
        json.dump(_srt_settings(root, egress_allowlist), f)
        settings_path = f.name
    try:
        r = subprocess.run(
            [srt, "-s", settings_path, "bash", "-lc", command],
            cwd=str(root), env=env, capture_output=True, text=True, timeout=timeout,
        )
        return _format(r)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"
    finally:
        try:
            Path(settings_path).unlink()
        except OSError:
            pass


# ---- none (explicit opt-out) ------------------------------------------------

def _run_none(command: str, root: Path, timeout: int) -> str:
    env = _jail_env(root)
    try:
        r = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=str(root), env=env, capture_output=True, text=True, timeout=timeout,
        )
        return "[UNSANDBOXED — no OS write-jail or egress boundary]\n" + _format(r)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"
