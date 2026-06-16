"""Prompt-file adapter — load a persona charter or prompt file as an agent."""
from __future__ import annotations
from pathlib import Path

from boundary.agent import Agent

HARNESS_NOTE = """

---

## Harness execution context (added by boundary)

You are running as a tool-calling agent inside `boundary`.

### Tools you have
- File system (workspace-jailed): `read_file`, `write_file`, `edit_file`,
  `list_dir`, `glob`, `grep` (with total counts), `count_matches`
- Shell (workspace cwd): `bash`
- Web: `fetch_url`
- Optional Clawpilot bridge, if enabled: `skill_list`, `skill_load`,
  `charter_list`, `charter_load`, `workiq`

### What you DON'T have
- `m_*` tools (m_remember, m_ask_user, m_get_skill RPC) — Scout-internal
- Browser MCP (`browser_navigate`) — not started in this process
- Interactive UI — your final assistant message is what the user sees

### Grounding rules (non-negotiable)
- **Every quantitative claim in your final answer** (counts, percentages,
  dollar amounts, dates) must trace to a specific tool result in this session.
  Cite the tool + file path. If you cannot cite, do not state the number.
- **Label every claim** with one of:
  - `[DATA]` — verified by a tool call in this session (preferred)
  - `[TRAINING]` — from prior knowledge, not verified here
  - `[HYPOTHESIS]` — inference or pattern match, not confirmed
  Never blend tiers in one sentence without flagging both.
- **Before your final message**, audit your draft: any unlabeled number gets
  either a verifying tool call now or a `[HYPOTHESIS]` downgrade.
- **`grep` returns total counts** in its header line — use those, don't
  extrapolate from the shown sample.
- **Verify writes**: read back any file you create before declaring done.
- **Be concise in the final message** — the user sees `--- final ---` plus
  your last assistant turn. No transcript replay.
"""


def load_persona(
    charter: str | Path,
    workspace: str | Path,
    *,
    name: str | None = None,
    client: str = "copilot",
    model: str | None = None,
    enable_web: bool = True,
    enable_clawpilot: bool = True,
    extra_system: str | None = None,
    **agent_kwargs,
) -> Agent:
    charter_path = Path(charter).expanduser()
    if not charter_path.exists():
        raise FileNotFoundError(f"charter not found: {charter_path}")
    charter_text = charter_path.read_text(encoding="utf-8")
    system_prompt = charter_text + HARNESS_NOTE
    if extra_system:
        system_prompt += "\n\n" + extra_system
    if name is None:
        parts = charter_path.parts
        if "agents" in parts:
            idx = parts.index("agents")
            if idx + 1 < len(parts):
                name = parts[idx + 1]
        if name is None:
            name = charter_path.stem
    client_kwargs = {"model": model} if model else {}
    return Agent(
        name=name,
        system_prompt=system_prompt,
        workspace=workspace,
        client=client,
        client_kwargs=client_kwargs,
        enable_web=enable_web,
        enable_clawpilot=enable_clawpilot,
        **agent_kwargs,
    )
