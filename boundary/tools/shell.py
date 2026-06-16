from __future__ import annotations

from boundary.tools.registry import ToolRegistry
from boundary.tools.sandbox import run_sandboxed
from boundary.tools.workspace import Workspace


def register_shell_tools(
    registry: ToolRegistry,
    workspace: Workspace,
    timeout: int = 60,
    allow: bool = True,
    driver: str = "seatbelt",
    egress_allowlist: list[str] | None = None,
) -> None:
    if not allow:
        return

    def _bash(command: str) -> str:
        return run_sandboxed(
            command,
            workspace_root=workspace.root,
            timeout=timeout,
            driver=driver,
            egress_allowlist=egress_allowlist,
        )

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
        return _bash(command)

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
        return _bash(command)
