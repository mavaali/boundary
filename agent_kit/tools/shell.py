from __future__ import annotations
import subprocess

from agent_kit.tools.registry import ToolRegistry
from agent_kit.tools.workspace import Workspace


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
        f"Run a bash command in the workspace directory. Timeout {timeout}s. WRITE TOOL — assume side effects; include 'reason'.",
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
        try:
            r = subprocess.run(
                command,
                shell=True,
                cwd=str(workspace.root),
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
