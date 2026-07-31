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
import sys
import tempfile
import threading
from pathlib import Path

# "auto" (the default) prefers the strongest sandbox available: srt's OS-enforced
# egress containment if installed, else macOS seatbelt's write-jail with a LOUD
# warning that egress is uncontained, else a hard error (never silently drop the
# jail). Explicit "srt" stays strict — a deliberate security choice fails loudly
# rather than degrading.
SANDBOX_DRIVERS = ("auto", "seatbelt", "srt", "nono", "none")

_WARNED_MESSAGES: set[str] = set()
_AUTO_WARNED_LOCK = threading.Lock()


def warn_once(message: str) -> None:
    """Emit `message` to stderr once per process — per distinct message, even
    under concurrent runs (batch best-of-K, multi-threaded scheduling). Deduping
    per message (not one global latch) so a fallback warning does not swallow an
    unrelated one (e.g. the deny_read-on-non-srt notice)."""
    with _AUTO_WARNED_LOCK:
        if message in _WARNED_MESSAGES:
            return
        _WARNED_MESSAGES.add(message)
    print(message, file=sys.stderr, flush=True)


def resolve_auto_driver() -> tuple[str | None, str | None]:
    """Resolve the 'auto' driver to a concrete one.

    Returns (driver, warning). driver is None when no sandbox is available (the
    caller turns that into an error). Prefers srt (egress contained); falls back
    to seatbelt on macOS (write-jail only — egress UNCONTAINED, hence the warning);
    refuses otherwise rather than running with no boundary.
    """
    if shutil.which("srt"):
        return "srt", None
    if platform.system() == "Darwin":
        return "seatbelt", (
            "[boundary] WARNING: sandbox driver 'auto' fell back to 'seatbelt' — srt "
            "is not installed, so network egress is NOT contained (exfiltration via "
            "bash is possible). For OS-enforced egress, install: "
            "npm i -g @anthropic-ai/sandbox-runtime, then use --sandbox-driver srt."
        )
    return None, None


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


def default_deny_read() -> list[str]:
    """A built-in set of common on-disk secret locations to hide from the jailed
    process. Resolved against the REAL home (before _jail_env repoints HOME), so
    these point at where credentials actually live. Only enforceable under srt
    (denyRead); seatbelt/none leave reads unrestricted."""
    home = Path.home()
    rel = [
        ".aws", ".ssh", ".gnupg", ".kube",
        ".config/gh", ".config/gcloud", ".config/git/credentials",
        ".docker/config.json", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    ]
    return [str(home / r) for r in rel] + ["/etc/shadow"]


def run_sandboxed(
    command: str,
    *,
    workspace_root: Path,
    timeout: int,
    driver: str = "auto",
    egress_allowlist: list[str] | None = None,
    deny_read: list[str] | None = None,
    credential_scopes: list | None = None,
) -> str:
    root = Path(workspace_root).resolve()
    if driver == "auto":
        resolved, warning = resolve_auto_driver()
        if warning:
            warn_once(warning)
        if resolved is None:
            return (
                "ERROR: no OS sandbox available — srt is not installed and seatbelt "
                "is macOS-only. Install srt (`npm i -g @anthropic-ai/sandbox-runtime`) "
                "for an egress-bounded jail, or pass --sandbox-driver none to run "
                "without any jail explicitly."
            )
        driver = resolved
    if deny_read and driver != "srt":
        warn_once(
            "[boundary] WARNING: deny_read/--deny-read is set but the active sandbox "
            f"driver is {driver!r}, which cannot restrict reads. Secret paths remain "
            "readable. Use --sandbox-driver srt to enforce the read denylist."
        )
    if driver == "seatbelt":
        return _run_seatbelt(command, root, timeout)
    if driver == "srt":
        return _run_srt(command, root, timeout, egress_allowlist or [], deny_read or [])
    if driver == "nono":
        return _run_nono(command, root, timeout, egress_allowlist or [], credential_scopes or [])
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

def _srt_settings(root: Path, egress_allowlist: list[str], deny_read: list[str]) -> dict:
    return {
        "network": {"allowedDomains": list(egress_allowlist), "deniedDomains": []},
        "filesystem": {
            # No allowRead: srt allows reads everywhere by default and allowRead
            # takes PRECEDENCE over denyRead, so allowRead:["/"] silently defeats
            # the read denylist (the F6 remediation was a no-op on macOS). Omit
            # it so denyRead actually binds. See #55 / F26.
            "allowWrite": [str(root)],
            "denyRead": list(deny_read),
            "denyWrite": [],
        },
    }


def _run_srt(command: str, root: Path, timeout: int, egress_allowlist: list[str],
             deny_read: list[str] | None = None) -> str:
    srt = shutil.which("srt")
    if not srt:
        return (
            "ERROR: srt not found. Install with `npm install -g @anthropic-ai/sandbox-runtime` "
            "or choose a different --sandbox-driver."
        )
    env = _jail_env(root)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
        json.dump(_srt_settings(root, egress_allowlist, deny_read or []), f)
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


# ---- nono (capability sandbox + credential-scoping proxy) -------------------

def _nono_command(command: str, root: Path, egress_allowlist: list[str],
                  credential_scopes: list, log_file: str | None = None) -> list[str]:
    """Build the `nono run` argv: fs write-jail (--allow), egress (--allow-domain
    / --block-net), and credential scoping (compile_nono_flags). Pure; unit-tested.

    nono denies reads by default, so secrets outside the workspace are hidden
    without an explicit denylist. The credential is phantom-injected upstream of
    the child (never in its env); out-of-scope endpoints 403. See spike doc."""
    from boundary.credential_proxy import compile_nono_flags

    cmd = ["nono", "run", "--allow", str(root), "--allow-cwd", "-s"]
    if log_file:
        cmd += ["--log-file", log_file]
    for domain in egress_allowlist:
        cmd += ["--allow-domain", domain]
    cmd += compile_nono_flags(credential_scopes)
    if not egress_allowlist and not credential_scopes:
        cmd += ["--block-net"]
    # Resolve bash to an absolute path in the PARENT (which has a full PATH).
    # nono's own binary resolution is PATH-sensitive and fails with
    # "cannot find binary path" when the child PATH is minimal; an absolute
    # path sidesteps that (matches the seatbelt/none drivers).
    bash = shutil.which("bash") or "/bin/bash"
    # -lc (login shell), matching the seatbelt/srt/none drivers: a login shell
    # sources /etc/profile and gets a full system PATH, so commands (env, curl,
    # git) resolve even when nono hands the child a minimal PATH. Plain -c left
    # the child with only nono's shim dir on PATH → "command not found".
    cmd += ["--", bash, "-lc", command]
    return cmd


def _run_nono(command: str, root: Path, timeout: int, egress_allowlist: list[str],
              credential_scopes: list) -> str:
    if not shutil.which("nono"):
        return (
            "ERROR: nono not found. Install nono (the capability sandbox) or "
            "choose a different --sandbox-driver."
        )
    # nono owns the jail and needs the REAL HOME for its own session state
    # (~/.local/state/nono) and keychain-backed credential resolution, so we do
    # NOT repoint HOME with _jail_env here (unlike srt/seatbelt/none).
    try:
        r = subprocess.run(
            _nono_command(command, root, egress_allowlist, credential_scopes),
            cwd=str(root), env=os.environ.copy(), capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return _format(r)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"


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
