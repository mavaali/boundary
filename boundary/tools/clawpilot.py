"""Tools that bridge boundary to the user's Clawpilot skill ecosystem.

These let a harness agent reach back into Scout-managed assets that happen to
live on disk or be callable via a CLI:
  - SKILL.md files under ~/.copilot/skills/ and ~/.copilot/m-skills/
  - persona charters under <workspace>/.squad/agents/*/charter.md
  - the workiq CLI (~/.copilot/bin/workiq) for M365 queries
"""
from __future__ import annotations
import subprocess
from pathlib import Path

from boundary.tools.registry import ToolRegistry

SKILL_DIRS = [
    Path("~/.copilot/skills").expanduser(),
    Path("~/.copilot/m-skills").expanduser(),
]
WORKIQ_BIN = Path("~/.copilot/bin/workiq").expanduser()


def _find_skill(name: str) -> Path | None:
    name = name.lower().lstrip("/")
    for base in SKILL_DIRS:
        if not base.exists():
            continue
        p = base / name / "SKILL.md"
        if p.exists():
            return p
    return None


def register_clawpilot_tools(
    registry: ToolRegistry,
    workspace_root: Path | None = None,
    allow_workiq: bool = True,
    workiq_timeout: int = 120,
) -> None:

    @registry.add(
        "skill_list",
        "List installed Clawpilot skills (~/.copilot/skills + ~/.copilot/m-skills). Returns one skill name per line.",
        {"type": "object", "properties": {}},
    )
    def skill_list() -> str:
        names: set[str] = set()
        for base in SKILL_DIRS:
            if not base.exists():
                continue
            for child in base.iterdir():
                if child.is_dir() and (child / "SKILL.md").exists():
                    names.add(child.name)
        return "\n".join(sorted(names)) if names else "(no skills found)"

    @registry.add(
        "skill_load",
        "Load the SKILL.md body for a named Clawpilot skill (e.g. 'competitiveresearch', 'adia'). Use this when your charter says 'invoke skill X' — read the SKILL.md and follow its instructions adapted to your available tools.",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    def skill_load(name: str) -> str:
        p = _find_skill(name)
        if p is None:
            return f"ERROR: skill not found: {name}"
        return f"[loaded {p}]\n\n" + p.read_text(encoding="utf-8")

    if workspace_root is not None:
        squad_dir = workspace_root / ".squad" / "agents"

        @registry.add(
            "charter_list",
            "List persona charters available in the workspace's .squad/agents/ directory.",
            {"type": "object", "properties": {}},
        )
        def charter_list() -> str:
            if not squad_dir.exists():
                return f"(no .squad/agents directory at {squad_dir})"
            names = sorted(
                d.name for d in squad_dir.iterdir()
                if d.is_dir() and (d / "charter.md").exists()
            )
            return "\n".join(names) if names else "(no charters)"

        @registry.add(
            "charter_load",
            "Load a persona charter from the workspace's .squad/agents/<name>/charter.md.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )
        def charter_load(name: str) -> str:
            p = squad_dir / name.lower() / "charter.md"
            if not p.exists():
                return f"ERROR: charter not found: {p}"
            return f"[loaded {p}]\n\n" + p.read_text(encoding="utf-8")

    if allow_workiq and WORKIQ_BIN.exists():
        @registry.add(
            "workiq",
            "Query Microsoft 365 data via workiq CLI. EXTERNAL — include 'reason'.",
            {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "reason": {"type": "string", "description": "Why this query is needed. Required."},
                },
                "required": ["question", "reason"],
            },
            kind="external",
        )
        def workiq(question: str, reason: str = "") -> str:
            try:
                r = subprocess.run(
                    [str(WORKIQ_BIN), "ask", "-q", question],
                    capture_output=True,
                    text=True,
                    timeout=workiq_timeout,
                )
                out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
                return f"[exit {r.returncode}]\n{out[-10000:]}"
            except subprocess.TimeoutExpired:
                return f"ERROR: workiq timed out after {workiq_timeout}s"
            except Exception as e:
                return f"ERROR: {type(e).__name__}: {e}"
