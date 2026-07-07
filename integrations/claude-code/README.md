# Boundary — Claude Code plugin

Enforce Boundary's envelope contract *inside a Claude Code session*, and grade the
run against it with a `boundary.third-umpire/v1` verdict.

Self-contained: Node.js only (no Python, no npm dependencies — every Claude Code
user already has Node). The Python `boundary` engine is an **optional** upgrade for
a richer verdict.

## What it enforces

Via a `PreToolUse` hook that hard-denies violations (and **defers** compliant calls
to Claude Code's normal permission flow — it blocks, it does not auto-approve):

- **Write allowlist** — `Write`/`Edit` to a path outside `writable_paths` is denied.
- **Write cardinality** — a write ceiling (`max_writes`) *and* a graded floor
  (`min_writes`, "enough must happen").
- **Staging pivot** — after `max_unstaged_reads` orientation reads, deep reads and
  all writes/commands are denied until you `/boundary:stage` a thesis. A refused
  write replays that thesis so you resume from it rather than restart.
- **Commit denylist** — `Bash` commands starting with `curl`/`wget`/`gh`/`git
  push|commit|tag`/… are denied.

At the end of each response (the `Stop` hook), a verdict is (re)written to `<cwd>/.boundary/verdict.json`:
`writes_inside_allowlist`, `produced_output` (the floor), `staging_pivot`,
`commit_denylist_held`, plus a post-hoc `summary.estimated_dollars` cost estimate.

## Install

Drop-in plugin (Node ≥ 18). Point Claude Code at this directory as a plugin, or
copy `integrations/claude-code/` into your plugins location. It bundles the hooks,
the `/boundary:stage` command, and its scripts — no build step.

Run the tests (no live Claude Code needed): `node --test integrations/claude-code/test/*.test.js`.

## Configure — `.boundary.json`

Place in your project root (absent → safe defaults):

```json
{
  "writable_paths": ["scratch/**", "docs/**"],
  "max_writes": 10,
  "min_writes": 1,
  "require_staging": true,
  "max_unstaged_reads": 3,
  "deny_commits": true
}
```

## Non-goals (so this isn't mistaken for the full engine)

- **Live spend enforcement / degrade / chargeback.** A hook cannot cap dollars
  mid-session — it is not handed token/cost data, and reconstructing it per call is
  impractical. Spend appears here only as a **post-hoc estimate, not a cap**
  (`summary.estimated_dollars`, or `"unavailable"` if the transcript can't be read).
- **Taint / information-flow.** Not tracked.
- **Prose-grounding checks** (numbers-grounded, claim-labels). The event log records
  tool decisions, not assistant text.

## Deliberate divergences from the Python engine

- **`[...]` globs in `writable_paths` are literals, not character classes.** The
  engine (fnmatch) treats `[ab]` as a class; this plugin matches it literally.
  `*` (within a segment), `**` (across segments), and `?` behave identically to the
  engine. Bracket classes in a *write allowlist* are effectively never used.
- **`Bash` is not counted against `max_writes`.** In Claude Code, Bash is not a
  first-class write tool and the hook sees only the pre-execution call.
- **Compliant calls defer** to Claude Code's permission flow (the plugin blocks
  violations; it does not auto-approve).

## Optional: richer verdict via the Python engine

If `boundary` (the `boundary-envelope` PyPI package) is on `PATH`, `verdict.js`
additionally transforms its event log into an engine transcript and runs
`boundary third-umpire … --format json`, attaching the fuller check suite under
`verdict.engine`. Token-dependent and prose checks there are inert (the plugin has
no token counts or assistant prose); the enforced-dimension checks are accurate.

## Validate against a live Claude Code (required before release)

The Claude Code integration was **live-tested and corrected**: blocking via **exit
code 2** + reason on stderr (not a JSON `permissionDecision`, which this CC ignores),
the **`Stop`** event for the verdict (there is no `SessionEnd`), and state co-located
under **`<cwd>/.boundary/state/`** — no `CLAUDE_PLUGIN_DATA` or session-id dependency.
Verified against real script subprocesses (only `cwd`, no env vars): unstaged /
off-allowlist / over-cardinality / commit calls exit 2 and block with the reason on
stderr; the Bash-invoked `stage-write.js` and the hooks agree on the cwd-keyed state
so staging works cross-process. Still worth confirming in a full interactive session:

- [ ] **A denied tool visibly does not execute**, and `permission_mode` doesn't
      pre-empt the exit-2 block — notably whether `bypassPermissions` mode ignores it
      (if so, document that enforcement is advisory in that mode).
- [ ] **Transcript carries per-turn token usage.** `summary.estimated_dollars` parses
      `transcript_path` for `message.usage` (`input_tokens`, `output_tokens`,
      `cache_read_input_tokens`, `cache_creation_input_tokens`). If the shape differs,
      adjust `lib/cost.js`'s reader (it degrades to `"unavailable"` rather than break).
- [ ] **The `Stop` hook fires and writes the verdict.** It runs each turn (rewriting
      `verdict.json`); the final one reflects the whole session.
- [ ] **Parallel tool calls** (Claude batching several reads/writes at once) don't race
      the counter in `state.json`. If the unstaged-read cap under-counts under batched
      reads, switch counters to derive from the append-only `events.jsonl`.
