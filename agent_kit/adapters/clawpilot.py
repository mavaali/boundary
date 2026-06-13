"""Clawpilot adapter — load a persona's charter.md (or any SKILL.md) and run it
as a tool-calling agent against a target workspace.
"""
from __future__ import annotations
from pathlib import Path

from agent_kit.agent import Agent

HARNESS_NOTE = """

---

## Harness execution context (added by agent-kit)

You are running as a tool-calling agent inside `agent-kit`. You have a sandboxed
file workspace, shell, web fetch, and bridges back to the user's Clawpilot
skill ecosystem.

### Tools you have
- File system (workspace-jailed): `read_file`, `write_file`, `edit_file`,
  `list_dir`, `glob`, `grep` (with total counts), `count_matches`
- Shell (workspace cwd): `bash`
- Web: `fetch_url`
- Clawpilot bridge: `skill_list`, `skill_load` (read any installed SKILL.md
  from `~/.copilot/skills` or `~/.copilot/m-skills`), `charter_list`,
  `charter_load`, `workiq` (Microsoft 365 query CLI)

### What you DON'T have
- `m_*` tools (m_remember, m_ask_user, m_get_skill RPC) — Scout-internal
- Browser MCP (`browser_navigate`) — not started in this process
- Interactive UI — your final assistant message is what the user sees

### Source hierarchy — use in this order
When researching anything in this workspace, query sources in this priority and
explicitly say which tier each finding came from:

1. **WIKI FIRST** — `Data Factory/wiki/` is the curated synthesis layer. Always
   start here. Useful entry points:
   - `Data Factory/wiki/WIKI.md` and `Data Factory/wiki/index.md` — navigation
   - `Data Factory/wiki/L0-primer.md` — foundational context (read for any
     Data Factory task)
   - `Data Factory/wiki/features/` — per-feature pages
   - `Data Factory/wiki/concepts/`, `syntheses/`, `cross-cutting/` — analyses
   - `Data Factory/wiki/decisions/`, `tensions/`, `open_questions/` — current state
2. **REPO SECOND** — raw specs and source docs under `Data Factory/`,
   `strategy/`, `compete/`, `Growth/`, `planning/`, `.squad/`. Use these to
   verify wiki claims or fill gaps the wiki doesn't cover yet.
3. **GENERAL SEARCH THIRD** — broad `glob`/`grep` patterns across `**/*.md`
   when the wiki + targeted repo paths come up dry. This is the fallback,
   not the default.

If a finding only shows up in tier 3, say so — it means the wiki is missing it.

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
