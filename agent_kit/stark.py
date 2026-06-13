"""Stark — the captain. Translates a loose user prompt into a structured
envelope spec, surfaces it for human approval, then dispatches.

This is the "coach selects the XI and game plan; captain sets the field"
layer between you and the agent runner.

Workflow:
    1. propose(user_prompt) -> EnvelopeProposal  (LLM-authored)
    2. show proposal to human; accept / edit / reject
    3. dispatch(proposal, persona_charter) -> EnvelopeRunResult
    4. grade with Fury
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from agent_kit.clients import make_client
from agent_kit.clients.base import Message
from agent_kit.envelope import Envelope, EnvelopeRunner
from agent_kit.adapters.clawpilot import load_persona

STARK_SYSTEM = """You are Stark — the captain. Your job is to translate a loose
user request into a TIGHT envelope spec that an agent will run inside.

You do NOT execute the task. You ONLY propose the envelope by calling the
`propose_envelope` tool exactly once.

Principles (from the user's hallucinated-intent doctrine):
- Specification quality is the bottleneck. Be precise.
- Reads are free; writes are gated. Always allowlist the minimum set of paths.
- Force agents to stop at ambiguity rather than interpolate.
- Pre-authorize the envelope. The agent runs inside it without further checks.

When proposing:
- `restated_intent` — restate the user's goal in 1-2 sentences. If the original
  prompt is ambiguous, name the ambiguity and your interpretation explicitly.
- `persona` — which persona is best suited (banner/vision/shuri/pepper/strange/
  fury/rhodey/natasha/uatu/thor/hope/scribe). Match capability to task.
- `writable_paths` — exact relative paths the agent may write. Avoid globs
  unless multiple files are genuinely required.
- `max_writes` — small. 1 is good. >5 needs justification in `rationale`.
- `min_writes` — usually 1 (forces the agent to produce something).
- `max_iters` — budget the work: research = 25-35, drafting = 15-25, editing = 10.
- `task` — a tightened version of the user's prompt: explicit deliverable,
  source hierarchy reminder, efficiency rules, label requirements.
- `clarifying_questions` — if and only if there is ambiguity the human MUST
  resolve before this envelope makes sense. Empty list is the common case.
- `rationale` — 1-3 sentences on why this envelope shape.

Be opinionated. The user can always edit your proposal."""

PROPOSE_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_envelope",
        "description": "Emit the structured envelope proposal. Call this exactly once.",
        "parameters": {
            "type": "object",
            "properties": {
                "restated_intent": {"type": "string"},
                "persona": {"type": "string", "description": "lowercase persona name"},
                "writable_paths": {"type": "array", "items": {"type": "string"}},
                "max_writes": {"type": "integer", "minimum": 1},
                "min_writes": {"type": "integer", "minimum": 0},
                "max_iters": {"type": "integer", "minimum": 5},
                "task": {"type": "string"},
                "clarifying_questions": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
            },
            "required": [
                "restated_intent", "persona", "writable_paths",
                "max_writes", "min_writes", "max_iters", "task", "rationale",
            ],
        },
    },
}


@dataclass
class EnvelopeProposal:
    restated_intent: str
    persona: str
    writable_paths: list[str]
    max_writes: int
    min_writes: int
    max_iters: int
    task: str
    rationale: str
    clarifying_questions: list[str] = field(default_factory=list)

    def to_envelope(self) -> Envelope:
        return Envelope(
            writable_paths=self.writable_paths,
            max_writes=self.max_writes,
            min_writes=self.min_writes,
        )

    def to_markdown(self) -> str:
        lines = [
            f"# Stark proposal",
            f"",
            f"**Restated intent:** {self.restated_intent}",
            f"**Persona:** {self.persona}",
            f"**Writable paths:** `{self.writable_paths}`",
            f"**Max writes:** {self.max_writes} | **Min writes:** {self.min_writes} | **Max iters:** {self.max_iters}",
            f"**Rationale:** {self.rationale}",
        ]
        if self.clarifying_questions:
            lines.append(f"\n**Clarifying questions (BLOCKING):**")
            for q in self.clarifying_questions:
                lines.append(f"- {q}")
        lines.append(f"\n**Task (tightened):**\n\n{self.task}")
        return "\n".join(lines)


class Stark:
    def __init__(self, client: str = "copilot", model: str = "claude-sonnet-4.5"):
        self.client = make_client(client, model=model)

    def propose(self, user_prompt: str, workspace_hint: str | None = None) -> EnvelopeProposal:
        user_msg = user_prompt
        context_blocks: list[str] = []
        if workspace_hint:
            wpath = Path(workspace_hint).expanduser()
            context_blocks.append(f"[workspace: {wpath}]")
            # Pull squad context if present — routing + team + conventions
            for candidate in [
                wpath / ".squad" / "routing.md",
                wpath / ".squad" / "team.md",
                wpath / ".squad" / "README.md",
                wpath / ".github" / "copilot-instructions.md",
                wpath / "CLAUDE.md",
            ]:
                if candidate.exists():
                    try:
                        text = candidate.read_text(encoding="utf-8")[:8000]
                        context_blocks.append(
                            f"\n--- {candidate.relative_to(wpath)} ---\n{text}"
                        )
                    except Exception:
                        pass
            # List available personas explicitly
            agents_dir = wpath / ".squad" / "agents"
            if agents_dir.exists():
                personas = sorted(
                    d.name for d in agents_dir.iterdir()
                    if d.is_dir() and (d / "charter.md").exists()
                )
                context_blocks.append(f"\n--- available personas ---\n{', '.join(personas)}")
        if context_blocks:
            user_msg = "\n".join(context_blocks) + f"\n\n--- USER REQUEST ---\n{user_prompt}"
        resp = self.client.chat(
            [
                Message(role="system", content=STARK_SYSTEM),
                Message(role="user", content=user_msg),
            ],
            tools=[PROPOSE_TOOL],
            tool_choice={"type": "function", "function": {"name": "propose_envelope"}},
        )
        if not resp.message.tool_calls:
            raise RuntimeError(
                f"Stark did not emit a propose_envelope call. content={resp.message.content!r}"
            )
        args = resp.message.tool_calls[0].arguments
        return EnvelopeProposal(
            restated_intent=args["restated_intent"],
            persona=args["persona"],
            writable_paths=args["writable_paths"],
            max_writes=int(args["max_writes"]),
            min_writes=int(args["min_writes"]),
            max_iters=int(args["max_iters"]),
            task=args["task"],
            rationale=args["rationale"],
            clarifying_questions=args.get("clarifying_questions", []) or [],
        )


def dispatch(
    proposal: EnvelopeProposal,
    workspace: str | Path,
    squad_dir: str | Path | None = None,
    client: str = "copilot",
    model: str | None = None,
    verbose: bool = False,
):
    """Run a proposal: load the persona charter, build an envelope, execute."""
    workspace = Path(workspace).expanduser()
    squad = Path(squad_dir).expanduser() if squad_dir else (workspace / ".squad" / "agents")
    charter = squad / proposal.persona / "charter.md"
    if not charter.exists():
        raise FileNotFoundError(f"persona charter not found: {charter}")
    agent = load_persona(
        charter=charter,
        workspace=workspace,
        client=client,
        model=model,
        enable_clawpilot=True,
        max_iters=proposal.max_iters,
    )
    runner = EnvelopeRunner(agent, proposal.to_envelope())
    try:
        return runner.run(proposal.task, verbose=verbose)
    finally:
        agent.close()
