# Codex orchestration plan — review and corrected architecture

Status: reviewed 2026-07-29. This replaces the draft "AI Orchestration Execution
Plan for codex" with a version whose mechanisms actually exist. Verdict on the
draft: the *shape* (inverted control, fixed budgets, Boundary as the execution
layer) is right and is largely what this repo already implements; three of the
concrete mechanisms were wrong (system-prompt tool registration, the API-key
"5-hour lockout" claim, and `/compact`-per-subtask).

## Claim-by-claim review of the draft

### 1. Financial framework — fixed dollar cap per workflow

**Sound, and already built.** Boundary treats spend as a fail-closed boundary:
`--envelope-max-dollars` caps a run, unpriced models are billed at the most
expensive known rate so the cap always binds, and cross-run budgets cover fleets
(see README "Spend control"). A $5 root-cause-analysis workflow is
`--envelope-max-dollars 5` — nothing new to build.

For the Claude Code leg specifically, `claude -p` accepts `--max-budget-usd`
per invocation, and `--output-format json` returns `total_cost_usd` so an
orchestrator can sum across calls. Codex CLI has no native dollar cap — the
orchestrator (or Boundary's envelope) must be the thing that enforces it.

### 2. Inverted control — Boundary executes, the model proposes

**Right idea, wrong wire protocol.** Stripping the CLI agent of direct OS
execution and routing all mutations through Boundary is exactly the separation
thesis this repo argues (`docs/separation-thesis.md`). But two corrections:

- **"Register Boundary as a discrete tool inside the Codex system prompt"** is
  the fragile version: prompt-registered tools depend on the model emitting
  parseable JSON and on nothing in context overriding the instruction. Both
  Codex CLI and Claude Code have a native tool layer — MCP. Expose Boundary as
  a local **MCP server** (stdio; it can wrap the same functions a REST endpoint
  would). One server then serves both CLIs:
  - Codex: `mcp_servers` in `~/.codex/config.toml`
  - Claude Code: `claude mcp add` / project `.mcp.json`
  A bare REST endpoint is fine as the internal implementation, but the
  registration surface should be MCP, not prose in a system prompt.

- **"Commits the diff to the TaintStore upon success"** misassigns the store.
  `boundary/taint.py`'s `TaintStore` is a provenance ledger — untrusted
  *sources* and *tainted files*, kept outside the workspace so the jailed agent
  can't clear it. Successful diffs belong in the run transcript/receipt; the
  TaintStore's job on a write is to *mark* the file tainted when the content
  derives from an untrusted source, not to archive the diff.

### 3. Claude Code deployment

**Both bullets need correction.**

- **API key ≠ "eliminate the 5-hour lockout".** The 5-hour rolling window is a
  property of Claude subscription plans (Pro/Max/Team), not of the CLI.
  Authenticating with `ANTHROPIC_API_KEY` doesn't lift a limit — it moves you to
  a different billing scheme: metered per-token spend, bounded by Anthropic
  Console workspace spend limits instead of rolling windows. That is the right
  choice for orchestration (deterministic cost, no shared window), but the cost
  is now unbounded-by-default — which is precisely why the envelope dollar cap
  must be on.

- **`/compact` after every subtask is an anti-pattern, and slash commands
  aren't how you drive headless runs.** `/compact` and `/clear` are real, but
  compaction *spends* tokens summarizing; doing it every subtask adds cost and
  loses detail. In an orchestrated setup each subtask should be a fresh
  `claude -p` invocation — that *is* the `/clear`. Claude Code auto-compacts
  when a long session approaches its context limit; use `--resume <session-id>`
  only when a subtask genuinely continues a prior one.

- **One more constraint the draft assumed away:** Claude Code cannot be
  registered as an MCP *server* inside Codex — it is an MCP client only. The
  orchestrator invokes it as a headless subprocess (`claude -p`), not as a tool
  endpoint.

## Corrected architecture

```
Orchestrator (your script; owns the workflow budget)
│
├─ spawns per subtask:  codex exec …            (Codex leg)
│                       claude -p … --output-format json   (Claude leg)
│        both launched with native exec tools stripped:
│        Claude:  --disallowedTools "Bash,Write,Edit"
│                 --allowedTools "mcp__boundary__*"
│        Codex:   sandbox mode read-only + boundary MCP tools
│
└─ Boundary MCP server (stdio, local)
      tools: boundary_read / boundary_stage / boundary_write / boundary_bash
      each call enforced by the envelope:
        write allowlist + floor/ceiling, staging pivot, commit denylist,
        --envelope-max-dollars (fail-closed pricing)
      on success: diff → run transcript/receipt;
                  provenance → TaintStore when input was untrusted
```

Budget enforcement is layered: `--max-budget-usd` per Claude invocation,
`--envelope-max-dollars` on the Boundary envelope as the fail-closed backstop,
and the orchestrator summing `total_cost_usd` across legs against the workflow
cap.

## Relation to the existing Claude Code plugin

`integrations/claude-code/` already ships the *mirror image* of this design:
instead of stripping Claude Code's tools and routing writes through Boundary, it
leaves the tools in place and uses a `PreToolUse` hook to hard-deny envelope
violations in-session. Its README is explicit about the trade-off: a hook cannot
cap dollars mid-session (spend is post-hoc estimate only) and taint is not
tracked. The MCP tool-inversion architecture above is the complement that
recovers both — live spend enforcement and taint recording — at the cost of the
agent losing native tool ergonomics. Keep both: the plugin for interactive
sessions a human is watching, tool-inversion for unattended orchestration.

## What "Gemini says I can't integrate Claude Code this way" gets right and wrong

Right: you can't drive it via system-prompt tool registration, manual slash
commands from an orchestrator, or as an MCP server inside Codex — and API-key
auth is a billing change, not a limit bypass. Wrong: every goal in the draft is
achievable with supported mechanisms — headless `claude -p`, `--disallowedTools`
/ `--allowedTools`, MCP for Boundary's tools, `--max-budget-usd`, and
`--output-format json` for cost accounting.
