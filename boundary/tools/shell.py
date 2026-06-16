from __future__ import annotations
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from boundary.tools.registry import ToolRegistry
from boundary.tools.workspace import Workspace


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


def _run_workspace_bash(command: str, workspace: Workspace, timeout: int) -> str:
    """Run bash with local file writes jailed to the workspace on macOS."""
    if platform.system() != "Darwin":
        return "ERROR: bash sandbox is only implemented on macOS (sandbox-exec unavailable)."
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return "ERROR: sandbox-exec not found; refusing to run unsandboxed bash."

    workspace_root = workspace.root.resolve()
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

    profile = _sandbox_profile(workspace_root)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".sb", delete=False) as f:
        f.write(profile)
        profile_path = f.name
    try:
        r = subprocess.run(
            [sandbox_exec, "-f", profile_path, "/bin/bash", "-lc", command],
            cwd=str(workspace_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return f"[exit {r.returncode}]\n{out[-8000:]}"
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"
    finally:
        try:
            Path(profile_path).unlink()
        except OSError:
            pass


def register_shell_tools(
    registry: ToolRegistry,
    workspace: Workspace,
    timeout: int = 60,
    allow: bool = True,
) -> None:
    if not allow:
        return

    @registry.add(
        "bash",
        f"Run a bash command in the workspace directory. Timeout {timeout}s. WRITE TOOL — assume side effects; include 'reason'. NOTE: commands starting with curl/wget/gh/az/mail/sendmail/osascript or 'git push|commit|tag' are refused as commit-class — use `bash_commit` for those.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "reason": {"type": "string", "description": "Why this command is needed. Required."},
            },
            "required": ["command", "reason"],
        },
        kind="write",
    )
    def bash(command: str, reason: str = "") -> str:
        return _run_workspace_bash(command, workspace, timeout)

    @registry.add(
        "bash_commit",
        f"Run a bash command for an IRREVERSIBLE external action (push, send, post, file). "
        f"COMMIT TOOL — gated by the envelope's on_commit policy (refuse / queue / ask / allow). "
        f"In headless runs this is REFUSED by default; the schedule YAML must opt in via "
        f"on_commit: allow with commit_allowlist including 'bash_commit'. Timeout {timeout}s. "
        f"Include 'reason' AND describe what state will change in the world.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "reason": {"type": "string", "description": "What state changes in the world and why. Required."},
            },
            "required": ["command", "reason"],
        },
        kind="commit",
    )
    def bash_commit(command: str, reason: str = "") -> str:
        return _run_workspace_bash(command, workspace, timeout)
