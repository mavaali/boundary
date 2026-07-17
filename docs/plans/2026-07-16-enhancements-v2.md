# Boundary — Enhancement Spec v2

> Source: landscape reassessment (Claude Managed Agents, Claude Code sandbox/hooks
> stack, MCP ecosystem) + discussion with Mihir, 2026-07-16.
> Audience: a coding agent (or human) implementing against this repo.
> Style contract: same as enhancements.md v1 — each item states the problem, the
> change, acceptance criteria, surfaces, a counter-argument, and a kill condition.
> Do not rescue a failing item. Land items in priority order.

---

## Standing constraint (applies to every item)

The **runner path stays first-class**: `boundary run` / `boundary schedule` with
the Copilot client and `scout_hook` events is the primary production deployment.
The governor (Item 3) and gateway (Item 4) are *additional frontends over the same
envelope semantics*, never replacements. Any item whose implementation degrades
the scheduled Scout path — behavior, latency, or event format — fails its own
acceptance regardless of its other criteria.

Rationale: Copilot-token inference makes scheduled runs subscription-cost, and
`scout_hook` is the integration contract with Scout. No platform harness
(Claude Code, Managed Agents) can serve either. The runner is not commoditized
*for this deployment*; it is only commoditized as a general-market product.

---

## Landscape summary (why these items, now)

- Claude Managed Agents (public beta, 2026-04) platformized the loop, sandbox,
  scoped permissions, and tracing; self-hosted sandboxes + MCP tunnels keep
  egress in-perimeter. Claude Code ships permissions + proxy-based network
  allowlist sandbox + programmable PreToolUse hooks.
- What no platform has: **task-shaped authorization semantics** — a typed
  portable envelope, a mid-run staging pivot, taint surfaced as a verdict,
  post-run property grading, envelope synthesis from a loose prompt.
- Therefore: keep the runner for Scout; extract the semantics so they can also
  govern other people's harnesses; and close the one channel nobody in the
  landscape models — cross-run persistence (memory poisoning).

---

## P0 — Load-bearing

### Item 0 — Extract the envelope policy kernel (enabler for 3 & 4)

**Problem.** Envelope semantics (spec, write accounting, staging gate, taint,
commit policy, umpire checks) live inside the runner (`envelope.py`, 869 lines,
coupled to the tool registry and loop). Items 3 and 4 need identical semantics
without the loop; forking them across three frontends guarantees drift.

**Change.** Factor a transport-agnostic policy kernel: a pure module that takes
envelope spec + a stream of typed events (`read`, `write`, `stage`, `commit`,
`egress`) and returns decisions (`allow` / `refuse` / `stage_required` /
`taint_flow`) plus an evidence log the Third Umpire consumes. The runner becomes
the first consumer; hooks adapter and MCP proxy become the second and third.
The envelope spec gains a versioned schema and a canonical hash (feeds Item 2
receipts).

**Acceptance.**
- `boundary selftest` green with the runner routed through the kernel; no
  behavior change (all 7 enforced guarantees hold).
- Kernel importable with zero model-client / httpx dependencies.
- Envelope spec serializes to a versioned document with a stable hash.
- The selftest fixture suite runs against the kernel directly (no agent loop).

**Surfaces.** `envelope.py`, `third_umpire.py` evidence ingestion, `selftest.py`.

**Counter-argument.** Refactor tax with no new user-visible capability.
Mitigation: it is the wall Items 2–4 hang on; without it the governor and
gateway re-implement (and eventually contradict) the runner's semantics.

**Kill.** If sandbox-driver process spawning is too entangled to express as
kernel decisions, scope the kernel to authz + taint + umpire evidence and leave
OS sandboxing runner-local. That is still sufficient for Items 3–4.

---

### Item 1 — Cross-run taint lineage (memory-poisoning defense)

**Problem.** Taint is run-scoped, but the Scout topology is a chain: scheduled
runs write summary files that later runs (and Scout itself) read. A tainted
Tuesday run can poison Wednesday's inputs — persistent prompt injection through
the workspace. No platform or neighbor models this channel; Boundary uniquely
owns both `history` and `schedule`, so it can.

**Change.**
1. `history` records, per run: the taint set and the list of written paths.
2. At read time, reading a file written by a tainted run inherits that taint
   (transitive, with lineage: `tainted_via: run 42 ← fetch_url`).
3. `scout_hook` events carry the taint lineage.
4. Third Umpire emits a `taint_lineage` verdict line distinct from same-run
   `taint_flow`.
5. Lineage clears through review: `boundary review approve <run>` marks that
   run's writes trusted (human inspection is the declassifier).

**Acceptance.**
- Run A (tainted via `fetch_url`) writes `notes.md`; run B reads `notes.md` →
  B is tainted with lineage naming A. Same behavior across a schedule boundary.
- A workspace-only chain (no tainted ancestor) never trips it — no false
  positive on the common case.
- `boundary review approve` on run A → run C reading `notes.md` is clean.
- `boundary history` shows a lineage column; scout_hook event includes it.

**Surfaces.** `history.py`, envelope read accounting (kernel after Item 0),
`third_umpire.py`, `headless.py` scout_hook payload, GUIDE security section.

**Counter-argument.** Taint accretes: within weeks every file in a busy
workspace has a tainted ancestor and the signal drowns. Mitigation: the
review-clears-lineage rule is the pressure valve, and default policy stays
`warn` — lineage is evidence for the umpire, not a block.

**Kill.** If, with review-clearing in normal use, Scout chains still saturate
to fully-tainted within a week, run-level lineage is too coarse — report and
escalate to per-file provenance sidecars as a separate design.

---

## P1 — The three-frontend expansion

### Item 2 — `gh` as typed commit tools + run receipts

**Problem.** `gh` sits on the kill-list, so agent PR workflows route around the
envelope instead of through it. And agent-authored PRs carry no attestation —
a reviewer cannot distinguish an enveloped run from a freehand one.

**Change.**
1. Promote 2–3 `gh` verbs to first-class commit tools (`gh_pr_create`,
   `gh_issue_comment`) under the existing `--on-commit` policy. Everything else
   `gh` stays denylisted.
2. Define the **run receipt**: `{envelope spec hash, Third Umpire verdict,
   downgrades, taint/lineage summary, run id}` rendered as a block in the PR
   body; `boundary receipt verify <run-id>` re-checks it against history.

**Acceptance.**
- Under `on_commit=ask`, `gh_pr_create` stages for human approval; under
  `refuse` it does not execute.
- A created PR body contains the receipt block; `receipt verify` round-trips.
- Receipts are emitted for runner runs and (post-Item 3) governed runs with the
  same schema.

**Surfaces.** `tools/` (new commit tools), commit policy, `history.py`,
`third_umpire.py` summary, GUIDE.

**Counter-argument.** Receipts are self-reported by the same machine that ran
the agent — not cryptographic provenance. True; state it. The receipt's value
is organizational (merge-gate + audit trail), not adversarial proof. Signing
can come later if a second party ever needs to verify.

**Kill.** None — pure addition.

---

### Item 3 — Claude Code governor (`boundary govern claude`)

**Problem.** Envelope semantics are unavailable to anyone running Claude Code,
whose native stack is tool/domain-granular (permissions, sandbox allowlist,
hooks) with nothing task-shaped: no staging pivot, no post-run grading, no
receipt.

**Change.** `boundary govern claude --task ...` compiles the envelope into a
Claude Code project config: permission deny-rules for the write allowlist,
a PreToolUse hook that calls the policy kernel (Item 0) per tool event, the
staging pivot enforced via a marker file in the workspace (deep reads/writes
denied by the hook until `stage_proposal` has been recorded — state lives on
disk, not in the hook process). Afterward, Third Umpire grades the Claude Code
JSONL transcript and emits the same receipt as a runner run.

**Acceptance.**
- A governed Claude Code run: write outside the allowlist → denied by hook.
- Deep read/write before staging → denied; after a valid stage marker → allowed.
- Third Umpire produces a verdict + receipt from the CC transcript.
- `boundary selftest` gains a governed-mode fixture subset.
- Runner path untouched (standing constraint).

**Surfaces.** new `govern` subcommand, kernel event adapter, transcript adapter
for CC JSONL, GUIDE new section.

**Counter-argument.** The governed agent may thrash against hook denials
(loop on refused writes) instead of staging. Mitigation: the hook's denial
message instructs the staging step explicitly — the same intent-nudge pattern
as `bash_commit`.

**Kill.** If hook-based denial cannot maintain a workable staging flow (agent
loops or the hook cannot see enough tool context to classify deep reads),
rescope to the Agent SDK `canUseTool` callback and report the hook limitation.

---

### Item 4 — MCP gateway (per-source taint at the trust boundary)

**Problem.** Run-level taint cannot say *which* source poisoned a run, and
Boundary's semantics only protect processes Boundary spawns. Meanwhile every
agent client speaks MCP.

**Change.** `boundary gateway` — an MCP proxy wrapping upstream servers for any
client. It labels each tool result with source provenance (server identity ×
configured trust), applies kernel authz to write/commit-shaped tools, and
streams evidence for a Third Umpire report. This is where taint graduates from
run-level to per-source.

**Acceptance.**
- A client calling an untrusted upstream through the gateway gets results
  recorded as tainted-by-that-source; a trusted upstream does not taint.
- A write-shaped tool call violating the envelope is refused at the proxy.
- Gateway emits an evidence log the umpire can grade without a transcript.

**Surfaces.** new `gateway` subcommand, kernel (Item 0), a provenance store
shared with Item 1's lineage model.

**Counter-argument.** Transparent MCP proxying (streaming, sampling, elicitation
passthrough) is finicky and the spec moves. Mitigation: support the minimal
profile (tools/list, tools/call) first; that covers the threat model.

**Kill.** If transparent proxying breaks mainstream clients in practice,
rescope to a sidecar *auditor* (observe + grade, no inline enforcement) and
report the delta in protection.

---

## P2 — Compounding value

### Item 5 — Shrink-to-fit envelopes (least-privilege mining)

**Problem.** Envelope authoring is manual and generous; scheduled runs repeat
the same task shape, so over-broad envelopes persist indefinitely.

**Change.** Replay past successful runs' tool-call sequences offline against
candidate tighter envelopes ("would `--envelope-max-writes 2` have broken any
of the last N green runs?"). Fielding Coach v2 proposes the minimal envelope
consistent with history; `boundary schedule tighten <name>` applies it with a
diff shown first.

**Acceptance.**
- `boundary envelope fit <schedule>` prints current vs proposed envelope with
  the replay evidence (N runs checked, 0 would-have-blocked).
- Applying a proposal never converts a previously-green run into a
  would-have-failed run (replay-verified).

**Surfaces.** `transcript.py` replay, `fielding_coach.py`, `schedule.py`.

**Counter-argument.** Past behavior under-predicts future need; a ratcheted
envelope will eventually block a legitimate new behavior. That is working as
intended — the block surfaces as a staged resume, not a lost run, which is
exactly the staging pivot's job.

**Kill.** If replay shows high would-have-blocked rates on green runs (task
shapes vary too much run-to-run), ratcheting is the wrong model — report the
false-block rate and stop.

---

### Item 6 — Quarantined umpire at the pivot (stage → clean review → unlock)

**Problem.** The same (possibly tainted) context that read an injection also
decides what to write; taint flags it but nothing independent looks before the
write budget opens.

**Change.** Optional `--pivot-review` mode: at `stage_proposal`, a separate
minimal-context model call (different client than the worker — e.g. Haiku or
OpenRouter vs the Copilot worker, genuine model diversity) reviews the staged
thesis + evidence against the original task. Write budget unlocks only on
approval; rejection resumes from the stage per the existing pivot semantics.
Dual-LLM-lite, attached to Boundary's one unique primitive.

**Acceptance.**
- With `--pivot-review`, a stage whose thesis contradicts the task (fixture)
  is rejected and writes stay locked; a benign stage unlocks normally.
- The reviewer call receives only {task, staged thesis, evidence citations} —
  never the raw tool transcript (that is the quarantine).
- Off by default; enabling it is visible in the umpire report (not a downgrade
  — an upgrade line).

**Surfaces.** staging gate (kernel), `clients/` selection, run/schedule config.

**Counter-argument.** One extra model call per run; a weak reviewer rubber-
stamps. Mitigation: cost is one cheap-model call; the reviewer's prompt is a
property check ("does thesis follow from citations, does it serve the task"),
not open-ended judgment.

**Kill.** If the quarantined reviewer cannot distinguish fixture-level
malicious stages from benign ones (rubber-stamp rate ≈ reject rate noise), the
review adds latency without protection — report and drop.

---

### Item 7 — A benchmark that measures the pivot

**Problem.** `benchmarks/results.md` honestly shows ASR delta = 0: current
models refuse naive injections unaided, so the harness proves nothing about
the envelope.

**Change.** Build the attack set frontier models actually fail, targeting the
mechanisms Boundary claims: indirect injection planted in workspace files
(read as trusted today — interacts with Item 1), exfil through *allowlisted*
sinks (targets taint), multi-turn drift that defeats plan-then-execute
(targets the pivot + Item 6). Report per-mechanism: which envelope dimension
caught it, not just aggregate ASR.

**Acceptance.**
- ≥1 attack class with undefended ASR > 0 on a current mid-tier model
  (Haiku-class), defended ASR lower, attributed to a named dimension.
- Results table updated with model + version pinned; negative results kept.

**Surfaces.** `benchmarks/suite.py`, `benchmarks/results.md`.

**Counter-argument.** Designing attacks that beat frontier refusal training is
real red-team work, not an afternoon. Correct — this is last on purpose, and
Items 1 & 6 create the mechanisms it measures.

**Kill.** If no attack class achieves undefended ASR > 0 on current models,
publish that as the finding ("model-level refusal currently dominates
envelope-level defense on this class") and freeze the benchmark until models
or attacks change. Do not manufacture a delta.

---

### Item 8 — Small gaps

- **Schedule fallback client.** Headless automation against
  `api.githubcopilot.com` via the Copilot OAuth app is gray-area under Copilot
  ToS and can be rate-limited or cut off without notice. Add
  `client_fallback:` to schedule YAML (e.g. copilot → openrouter) so a Copilot
  cutoff degrades to metered inference instead of silencing Scout.
  *Acceptance:* kill the Copilot token mid-schedule (fixture) → run completes
  on the fallback client and the umpire report names the failover.

---

## Dependency order

```
Item 0 (kernel) ──┬──> Item 3 (Claude Code governor)
                  ├──> Item 4 (MCP gateway)
                  └──> simplifies Items 1, 2, 6

Item 1 (lineage) ────> Item 7 (workspace-injection attack class)
Item 2 (gh + receipts) ──> Item 3 emits the same receipts
Item 6 (pivot review) ──> Item 7 (drift attack class)
Items 5, 8 independent.
```

## One-line summary for the implementer

Keep the runner as the Scout production path; extract its semantics into a
kernel so the same envelope can govern Claude Code and proxy MCP; close the
cross-run persistence channel nobody else models; then build the benchmark
that measures the pivot instead of re-measuring model refusal training.
