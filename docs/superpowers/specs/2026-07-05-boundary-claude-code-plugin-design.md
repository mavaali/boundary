# Boundary — Claude Code plugin (design spec)

**Date:** 2026-07-05 · **Status:** design, pre-implementation.

## Goal

A Claude Code plugin named `boundary` that enforces Boundary's envelope contract
*inside a Claude Code session* and grades the run against it. Two aims, jointly:

- **Defense (kill-condition):** make the differentiated pieces — the **staging
  pivot** and a **`boundary.third-umpire/v1` verdict** — real in Claude Code, so a
  harness *adopts* the contract vocabulary instead of a hooks+`srt` clone replacing
  the engine. The plugin is evidence the contract is a portable standard, not an API
  to one binary.
- **Practical utility:** deliver enforcement a Claude Code user actually wants —
  a write jail, a write-count cap, a commit denylist, and a post-run report card
  (verdict + a cost estimate) — at zero-friction install.

Self-contained enforcement (no Python required); the Python engine is an *optional*
upgrade for the richer verdict only.

## Non-goals (stated so they are not silent gaps)

- **Live spend *enforcement* / degrade / chargeback.** Enforcing a dollar cap
  *mid-session* needs per-call token totals. Hooks are not handed token/cost data
  directly — the clean fix (anthropics/claude-code#11008) has sat untouched for
  months on a fast-moving product, so it is not a bet to build on — and
  reconstructing totals by re-parsing a growing transcript on *every* tool call is
  O(n²), one call lagged, and fragile. So live caps, degrade-to-cheaper, and
  per-tenant chargeback are out — as impractical-in-a-hook, not as blocked-on-a-promise.
  **However, post-hoc spend *visibility* is a cheap, different thing and IS in scope**
  (one parse at session end) — see "Spend visibility" under Verdict.
- **Taint / information-flow.** Deferred — needs untrusted-read → write tracking that
  is complex and version-fragile without the spend story to motivate it.
- **Prose-grounding checks** (numbers-grounded, claim-labels). The plugin's event log
  captures tool decisions, not assistant text, so these are only reachable via the
  optional engine path over Claude Code's own transcript (whose format the docs warn
  changes between versions) — out of the self-contained MVP.

## Runtime decision

Hook scripts are **Node.js with zero external dependencies** (Node built-ins only:
`fs`, `process`, `child_process`). Rationale: every Claude Code user already has Node
(CC ships as an npm package), so this is genuinely "no extra dependency" — no `jq`, no
bundled interpreter, no Python. Cross-platform. The Python `boundary` CLI stays
optional, invoked only if present on `PATH`.

## Architecture

A CC plugin directory published to a plugin marketplace, living in this monorepo at
`integrations/claude-code/` so the verdict schema stays shared with the engine.

```
integrations/claude-code/
├── .claude-plugin/plugin.json     # manifest (name: boundary)
├── hooks/hooks.json               # SessionStart, PreToolUse, SessionEnd
├── scripts/
│   ├── start.js                   # SessionStart: read config, init state, emit envelope_start
│   ├── enforce.js                 # PreToolUse: gate + count + log a Boundary-schema event
│   └── verdict.js                 # SessionEnd: grade events.jsonl -> boundary.third-umpire/v1
├── commands/ (or skills/)         # /boundary:stage, /boundary:start
├── lib/                           # pure logic shared + unit-tested (envelope.js, grade.js, cost.js)
└── test/                          # node --test unit + e2e fixtures
```

### Envelope declaration

A `.boundary.json` in the project `cwd` declares the session envelope; absent → safe
defaults (staging on, a conservative `writable_paths`, `max_writes`, `deny_commits`).

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

### State

Per-session state under `${CLAUDE_PLUGIN_DATA}/<session_id>/`, keyed by the stable
`session_id` present in every hook input:

- `state.json` — counters (`writes_executed`, `unstaged_reads`), `staged` flag.
- `staged.json` — the staged thesis (thesis / hypotheses / evidence_plan / intended_write).
- `events.jsonl` — the plugin's **own** enforcement log the hooks append as they run:
  one line per decision, discriminated by a `kind` field
  (`envelope_start`, `write_allowed`, `write_refused`, `staged`, `limit_hit`,
  `bash_commit_blocked`, `envelope_end`) matching the engine's event `kind` vocabulary.
  This is the self-contained verdict's input — enforcement and verdict *grading* never
  parse Claude Code's own transcript, sidestepping its version-fragile format. (Two
  narrow, gracefully-degrading exceptions read the transcript: the post-hoc cost
  estimate and the optional engine path — see Verdict.) **Note:** this flat log is the
  plugin's private shape; it is *not* what the engine ingests (see Verdict below).

## Enforcement (PreToolUse → `enforce.js`)

Matcher: `Write|Edit|Bash|Read|Grep`. Reads envelope + state + the tool call, applies
in order, returns `permissionDecision: "deny"` with a `permissionDecisionReason`, and
appends the corresponding event:

1. **Unstaged-read cap** — on `Read|Grep` when `require_staging` and not staged and
   `unstaged_reads >= max_unstaged_reads`: deny, reason instructs `/boundary:stage`.
2. **Staging gate** — on `Write|Edit|Bash` when `require_staging` and not staged: deny,
   reason instructs staging first.
3. **Write allowlist** — on `Write|Edit` whose path is outside `writable_paths`
   (normalized, `..`/absolute rejected): deny.
4. **Cardinality** — on `Write|Edit` when `writes_executed >= max_writes`: deny.
5. **Commit denylist** — on `Bash` whose command's argv[0] basename is a commit binary
   (`curl`, `wget`, `gh`, `git push|commit|tag`, `mail`, `osascript`, …): deny.

Allowed writes increment `writes_executed`. Non-matched or allowed calls return
`permissionDecision: "allow"`/`"defer"`.

## Staging pivot (`/boundary:stage` + deny-reason re-anchor)

The `/boundary:stage` command records the staged thesis to `staged.json` and appends a
`staged` event. The pivot is enforced as the **strong form**, matching every part of the
engine that is *itself* enforced (not merely prompted):

- **Hard-enforced:** the unstaged-read cap and the write/commit gate (both deny via
  hooks), identical to `EnvelopeRunner`.
- **Resume-from-thesis:** on *every* refused write, `enforce.js` replays the **full
  staged thesis + evidence plan + "resume from this stage; do not restart research"**
  in `permissionDecisionReason`. Deterministic re-injection, not a vague nudge.

**Honest fidelity caveat.** A hook is *reactive per tool-event*; it cannot compose the
model's conversation turns the way the engine owns its loop. So it **deters rather than
structurally forbids** a restart — it denies the tools a restart would use and
re-anchors on the thesis, but cannot guarantee the model won't try. Note: the engine's
own post-stage read discipline is *also* prompt-guided (`envelope.py` does not
mechanically gate post-stage reads), so the real-world gap is a re-anchor nudge vs. a
guaranteed-pinned context — narrow.

## Verdict (SessionEnd → `verdict.js`)

Appends an `envelope_end` line to `events.jsonl`, then grades it into a
`boundary.third-umpire/v1` document (same schema id as the engine). The plugin owns
both the writer (`enforce.js`) and this self-contained reader (`lib/grade.js`), so it
defines its own check names over the enforced dimensions it directly observed:

- `writes_inside_allowlist` (any `write_refused` for path → fail)
- `produced_output` (`writes_executed >= min_writes` → the liveness floor)
- `staging_pivot` (a `staged` event exists, before the first write)
- `commit_denylist_held` (any `bash_commit_blocked` event)

Output written to `.boundary/verdict.json` + a one-line summary.

### Spend visibility (post-hoc cost estimate)

Separate from the walled *enforcement*, `verdict.js` recovers the visceral cost number.
At `SessionEnd` it parses the Claude Code transcript (path supplied in the hook input)
for the per-turn token usage CC records, prices it with a bundled rate card (same axes
as the engine — input / cached / cache-write / output), and writes the estimate into
the verdict's free-form `summary` as `estimated_dollars` — **the same field the engine's
own summary already carries** — so it is purely additive and claims no new
`boundary.third-umpire/v1` schema surface. Surfaced as e.g. *"~$0.28 this session
(≈45k in / 7k out)"*.

This is an **estimate, not an enforced cap**, and is the **one** place the plugin parses
Claude Code's own transcript — a single, contained version-fragility point. If the
transcript shape changes or token fields are absent, the cost line degrades to
`"unavailable"`; it never fails the verdict or blocks a run.

**Assumption to validate before building:** that CC's transcript carries per-turn token
usage in a parseable shape (it should — `/cost` derives session cost from it). Confirm
against a live CC version; if false, spend visibility drops and the enforced-dimension
verdict is unaffected.

**Optional engine upgrade — requires a transcript transform, not the flat log.** The
engine's `ThirdUmpire.grade()` does **not** read a flat `events.jsonl`; it reads a
*transcript* whose lines are discriminated by a `type` field and pulls the enforcement
events from a **nested** list at `envelope_end["events"]` (each `{kind, tool, detail,
iteration}`). So if `boundary` is on `PATH`, `verdict.js` must first **transform** its
`events.jsonl` into an engine-shaped transcript:

- an `{"type":"envelope_start", writable_paths, min_writes, max_writes, require_staging, …}` line,
- an `{"type":"envelope_end", writes_executed, writes_attempted, …, "events":[ …the plugin's enforcement events as {kind,tool,detail,iteration}… ]}` line,
- an `{"type":"end","iterations":N}` line,

then run `boundary third-umpire <transcript> --format json` and link the richer verdict.
Token-dependent checks (e.g. `spend_pacing`) and prose checks (grounding, claim-labels)
will be inert — the plugin has no token counts and no `assistant`/`tool_result` prose
lines — but the enforced-dimension checks are accurate. This transform is a small,
bounded, independently-testable task; the self-contained verdict above is the source of
truth and does not depend on it.

## Data flow

```
SessionStart  → start.js   : read .boundary.json → state.json + envelope_start event
Read/Grep     → enforce.js : unstaged-read cap → allow/deny + event
Write/Edit    → enforce.js : stage gate → allowlist → cardinality → allow/deny + event
Bash          → enforce.js : stage gate → commit denylist → allow/deny + event
/boundary:stage           : write staged.json + staged event
SessionEnd    → verdict.js : envelope_end → grade events.jsonl + parse transcript for cost estimate → verdict.json (+ engine if present)
```

## Testing

`lib/` holds the pure logic: `decide(envelope, state, toolInput) → {decision, reason,
event, newState}` and `grade(events) → verdict`. Both are pure functions of their
inputs — unit-tested with `node --test` by feeding synthetic hook-input JSON and state,
asserting the decision, the emitted event, and the verdict. No live Claude Code needed
(same mock philosophy as `benchmarks/`). The thin `scripts/*.js` only do stdin/stdout +
file I/O around `lib/`. The cost estimator is likewise pure —
`estimateCost(transcriptLines, rateCard) → {dollars, in_tok, out_tok}` (where `dollars`
prices all four rate-card axes — input / cached / cache-write / output — and
`in_tok`/`out_tok` are display aggregates) — tested by feeding a synthetic transcript
with token fields and asserting the estimate, plus a case with token fields absent
asserting the graceful `"unavailable"` degrade.

**End-to-end fixture:** replay a session — `SessionStart` → unstaged-`Read`×4 (4th
denied) → `/boundary:stage` → `Write` allowed → out-of-allowlist `Write` denied →
`Write`×N until cardinality denied → `Bash curl` denied → `SessionEnd` — and assert the
final `boundary.third-umpire/v1` verdict (e.g. `staging_pivot` pass, `produced_output`
pass, `writes_inside_allowlist` fail).

## Distribution

Published as a Claude Code plugin (marketplace entry / installable from the repo),
separate from the PyPI `boundary-envelope` package. The README states the non-goals
(live spend *enforcement* / taint) up front — and that spend appears as a post-hoc
*estimate*, not a cap — so the enforcement envelope is not mistaken for the full engine.

## Risks / open questions

- **`${CLAUDE_PLUGIN_DATA}` lifecycle** is not documented (TTL/retention across
  sessions). Mitigation: key state on `session_id` and treat stale dirs as best-effort;
  `verdict.js` cleans up its session dir after emitting.
- **`permission_mode` interactions** (e.g. `bypassPermissions`) may pre-empt a `deny`.
  Verify against a live version; document any mode where enforcement is advisory.
- **CC transcript format drift** is avoided for enforcement/verdict (we author our own
  log). Two deliberate exceptions parse CC's transcript: spend visibility (one parse at
  `SessionEnd`) and the *optional* engine path — both contained, both degrade gracefully.
- **Transcript token availability** (spend visibility) is *assumed, not verified here*:
  that CC's transcript records per-turn token usage in a parseable form. Validate against
  a live version before building the estimator; the cost line degrades to `"unavailable"`
  if absent, never blocking.
- **SessionEnd on abrupt termination** may not fire; the verdict is best-effort. The
  event log still exists for a later manual verdict — self-contained via `lib/grade.js`,
  or via `boundary third-umpire` after the same transcript transform (never on the flat
  log directly).
