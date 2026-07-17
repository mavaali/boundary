# Boundary — Enhancement Spec v3

> Supersedes the v2 spec (2026-07-16, which shipped only on the closed PR #40
> branch). Audited against `main` @ `a8c2af1` (v0.12.0 + Claude Code plugin,
> 2026-07-17). Style contract unchanged from v1/v2: each item states the
> problem, the change, acceptance criteria, surfaces, a counter-argument, and a
> kill condition. Do not rescue a failing item. Land in priority order.
>
> **Freshness rule (learned the hard way):** this repo's `main` moves fast,
> partly via parallel agent sessions. Before starting ANY item, `git fetch
> origin`, re-read `CHANGELOG.md` [Unreleased], and re-verify the item's
> "Upstream status" line still holds. PR #40 died because its branch was cut
> from month-stale state.

---

## Standing constraint (applies to every item)

The **runner path stays first-class**: `boundary run` / `boundary schedule`
with the Copilot client and `scout_hook` events is the primary production
deployment. New frontends (CC plugin, MCP gateway) are additional adopters of
the contract, never replacements. Any item whose implementation degrades the
scheduled Scout path — behavior, latency, or event format — fails its own
acceptance regardless of its other criteria.

---

## What happened to v2 (the honest ledger)

| v2 item | Upstream status @ a8c2af1 | v3 disposition |
|---|---|---|
| 0. Policy-kernel extraction | Not done in Python — but `docs/boundary-contract-spec.md` made the stronger move: the contract as harness-independent *vocabulary*, with two independent implementations (Python engine; JS plugin `integrations/claude-code/`) | **Reframed** → Item 0: conformance suite (the vocabulary now needs a mechanical check that its implementations agree) |
| 1. Cross-run taint lineage | **Superseded, better**: `boundary/taint.py` TaintStore — file-granular, persisted outside the workspace (tamper-resistant), cross-run by construction; `boundary taint --show/--clear` | Dropped. Residual gap (plugin tracks no taint) noted in Item 3 |
| 2. gh commit tools + receipts | Absent. Enablers shipped: `ThirdUmpireReport.as_dict()/to_json()` (0.12.0) and `Envelope.spec_dict()/spec_hash()` (PR #41) | **Kept**, split into Items 1 (receipts) and 2 (gh tools) |
| 3. Claude Code governor | **Shipped** as the JS plugin (#38): write allowlist, cardinality ceiling+floor, staging pivot, commit denylist | Residual: config bridge + verdict names its policy → Item 3 |
| 4. MCP gateway | Absent | Kept, P2 (second external adopter of the contract) |
| 5. Shrink-to-fit envelopes | Partially adjacent: `boundary/retry.py` does *reactive* FAIL→retighten per dispatch | Kept, P2, rescoped as the *proactive* complement reusing retry's tighten primitives |
| 6. Quarantined pivot reviewer | Absent (best-of-K judge in `multirun.py` is post-run selection, not mid-run stage review) | Kept, P2, reusing judge plumbing |
| 7. Pivot-measuring benchmark | Largely superseded: three-regime suite + `drip_exfil_over_writes` accumulation task + separation-thesis doc; mock-harness ASR 4/4 → 2/4 → 0/4 | Rescoped → Item 6: the *live-model* number |
| 8a. Fallback client | Absent (`degrade_to` is cost-triggered model swap; `headless_fallback` is best-of-K plumbing — neither handles auth/provider failure) | Kept as small gap → Item 7 |

---

## P0 — Make the contract thesis load-bearing

### Item 0 — Contract conformance suite (engine ↔ plugin anti-drift)

**Upstream status:** `docs/boundary-contract-spec.md` Part A declares the
invariant ("a conforming implementation MUST refuse, not merely warn, on the
tool-layer fields"); nothing mechanically checks that the Python engine and the
JS plugin implement the same semantics. They already differ in surface (plugin:
no taint, no spend caps, CC tool names) and will drift further.

**Problem.** The separation thesis and contract spec stake Boundary's value on
the *contract*, with implementations as adopters. Two implementations with no
shared test oracle is how a vocabulary quietly becomes two dialects — and a
verdict from one implementation stops meaning what the other enforces.

**Change.** A harness-neutral conformance fixture set, checked into the repo
and run against both implementations in CI:
1. `conformance/fixtures/*.json` — each fixture: an envelope (contract-spec
   field subset), a sequence of contract-level tool events (`write path=…`,
   `read`, `bash cmd=…`, `commit`, `stage`), and the expected decision per
   event (`allow` / `refuse`) + expected verdict-layer outcome.
2. A Python adapter driving the engine's enforcement path over the fixtures
   (pytest).
3. A Node adapter driving the plugin's `enforce.js` decision surface over the
   same files (`node --test`, matching the plugin's existing zero-dependency
   test setup).
4. Fixtures cover only the **shared field subset** (allowlist, ceiling,
   staging, commit denylist); each fixture declares `requires:` capabilities so
   engine-only fields (taint, spend) run engine-side without falsely failing
   the plugin.

**Acceptance.**
- The same fixture files drive both adapters; both pass in CI (pytest job +
  `node --test` job).
- Deliberately flipping one enforcement rule in either implementation fails
  that implementation's conformance run (demonstrated once in the PR).
- The contract-spec doc gains a "Conformance" section pointing at the fixtures.

**Surfaces.** new `conformance/`, `tests/test_conformance_engine.py`,
`integrations/claude-code/test/conformance.test.js`, CI workflow, contract-spec doc.

**Counter-argument.** Two adapters is real maintenance. True — but the adapters
are thin (map contract verbs to each implementation's call surface), and the
alternative is the contract doc silently rotting into aspiration.

**Kill.** If the plugin's decision surface can't be driven headlessly from
fixture data (hard dependency on live Claude Code hook context beyond what
`hooks.json` passes), scope conformance to the engine plus plugin unit tests
that *reference* the same fixture files, and report the gap in the
contract-spec doc.

---

### Item 1 — Run receipts (`boundary.receipt/v1`)

**Upstream status:** both halves exist — `Envelope.spec_dict()/spec_hash()`
(PR #41) and the exportable verdict `boundary.third-umpire/v1` (0.12.0) — but
nothing binds them: a verdict does not name the policy it graded against.

**Problem.** A verdict without the policy hash is "the run was graded" —
against what, is on trust. The receipt is the portable claim: *this run
executed inside this exact envelope, and here is the grade*.

**Change.**
1. Receipt document, schema `boundary.receipt/v1`: `{spec_version, spec_hash,
   spec: spec_dict(), verdict: third-umpire/v1 doc, run_id, schedule_name,
   model, estimated_dollars, transcript_path, created_at}`.
2. Every headless run emits one (history column + optional file next to the
   transcript); interactive envelope runs emit best-effort like the existing
   adhoc ledger row.
3. `boundary receipt show <run-id>` prints it; `boundary receipt verify
   <run-id>` re-hashes the embedded spec (hash matches `spec_hash`) and
   re-grades the transcript (verdict matches), exiting non-zero on mismatch.
4. `scout_hook` event gains `receipt` (path or inline) so Scout can gate on it.

**Acceptance.**
- A scheduled run produces a receipt whose `spec_hash` equals
  `Envelope.spec_hash()` of the envelope that ran.
- `receipt verify` round-trips; tampering with the stored spec or transcript
  makes it exit non-zero.
- Selftest gains a `receipt_verifies` guarantee.

**Surfaces.** `headless.py`, `history.py` (column + migration), `cli.py`,
`selftest.py`, GUIDE.

**Counter-argument.** Self-reported by the machine that ran the agent — not
cryptographic provenance. Correct; say so in the doc. The value is
organizational (audit trail, CI/merge gating), and signing can be added later
without changing the schema.

**Kill.** None — additive composition of two shipped artifacts.

---

## P1 — Delivery vehicles

### Item 2 — Typed `gh` commit tools; receipts land in PR bodies

**Upstream status:** absent. The kill-list comment in `envelope.py` already
names this as the sanctioned path: "if an agent shells out to `gh` repeatedly,
the answer is a typed gh_* commit tool, NOT a longer denylist."

**Problem.** `gh` on the bash denylist means agent PR workflows route around
the envelope instead of through it, and agent-authored PRs carry no
attestation.

**Change.** Two or three typed `kind="commit"` tools — `gh_pr_create`,
`gh_issue_comment` — under the existing `on_commit` policy
(refuse/queue/ask/allow + allowlist). `gh_pr_create` appends the run's receipt
(Item 1) as a fenced block in the PR body. Raw `gh` via bash stays denylisted;
the denylist does not grow.

**Acceptance.**
- Under `on_commit=refuse` the tools are refused; under `queue` they halt for
  review; under `allow` + allowlist they execute (mock `gh` in tests — no
  network in CI).
- A created PR body contains the `boundary.receipt/v1` block.
- Denylist entry count unchanged (12-cap rule intact).

**Surfaces.** `boundary/tools/` (new module), tool registration in
agent/persona loading, GUIDE commit-tools section.

**Counter-argument.** `gh` has a huge surface; typing two verbs invites "add
one more" forever. Mitigation: same slope guardrail as the denylist — a hard
cap (3 typed gh verbs) written into the module docstring; past it, the answer
is an MCP gateway (Item 4), not more tools.

**Kill.** None — pure addition behind existing policy.

---

### Item 3 — Plugin ↔ engine config bridge (the verdict names its policy)

**Upstream status:** the plugin reads hand-authored `.boundary.json` and its
`verdict.json` names no policy identity. Its README's non-goals (no taint, no
spend) are honest but unmachine-readable.

**Problem.** Two consequences of the gap: (a) a plugin verdict can't
participate in receipts — nothing says which envelope it enforced; (b) the
`.boundary.json` dialect can drift from `Envelope` semantics with no tripwire
(this is Item 0's drift, on the config axis).

**Change.**
1. `boundary export cc-plugin [--schedule <yaml>|flags]` generates
   `.boundary.json` from an `Envelope`, emitting only the plugin-supported
   field subset plus `{"spec_hash": …, "enforced_fields": […]}` — the hash of
   the FULL spec it was derived from, and the honest list of what this
   frontend enforces.
2. The plugin's `verdict.json` copies `spec_hash` + `enforced_fields` through,
   so a plugin verdict is receipt-compatible with its enforcement scope
   declared.
3. Contract-spec doc: a short "partial implementations" clause — a conforming
   partial implementation MUST declare `enforced_fields`.

**Acceptance.**
- `export cc-plugin` output validates against the plugin's config reader
  (round-trip test in the plugin's node tests).
- A plugin session's `verdict.json` contains the `spec_hash` and
  `enforced_fields` from its config.
- Engine-side test: exported subset values equal the source Envelope's.

**Surfaces.** `cli.py`, `integrations/claude-code/lib/envelope.js` +
`scripts/verdict.js`, contract-spec doc.

**Counter-argument.** A bridge is one more thing to keep in sync. Yes — which
is why its round-trip test lives in the conformance suite (Item 0) so the sync
is checked, not assumed.

**Kill.** If plugin maintainership diverges (JS side stops tracking engine
fields), stop generating and declare `.boundary.json` a frozen v1 dialect in
the contract spec instead.

---

## P2 — Compounding value

### Item 4 — MCP gateway (second external adopter; per-source taint)

**Upstream status:** absent. TaintStore now provides the persistence substrate
a gateway can write into.

**Change.** `boundary gateway` — an MCP proxy for any client: labels each
upstream tool result with source provenance (server × configured trust,
feeding TaintStore), applies contract authz to write/commit-shaped tools,
emits an evidence log the Third Umpire can grade → receipt-compatible.
Minimal profile first (`tools/list`, `tools/call`).

**Acceptance.** Untrusted-upstream results marked in TaintStore; envelope-
violating write refused at the proxy; umpire grades the gateway evidence log;
conformance fixtures (Item 0) run against the gateway adapter too.

**Counter/Kill.** As v2: if transparent proxying breaks mainstream clients,
rescope to sidecar auditor and report the protection delta.

---

### Item 5 — Shrink-to-fit envelopes (proactive least-privilege mining)

**Upstream status:** `retry.py` tightens *reactively* after a FAIL verdict,
per dispatch. Nothing mines history to tighten schedules that keep passing
with slack.

**Change.** `boundary envelope fit <schedule>`: replay the last N green runs'
transcripts against candidate tighter envelopes (reusing retry.py's tighten
primitives as the candidate generator, inverted: tighten-until-a-green-run-
would-have-broken, then step back one notch). Prints current vs proposed with
replay evidence; `--apply` rewrites the schedule YAML.

**Acceptance.** Proposal never converts a previously-green run into
would-have-blocked (replay-verified); a schedule with genuine slack (fixture)
gets a strictly tighter proposal; one without gets "already minimal".

**Kill.** If replay shows high would-have-blocked variance across green runs
(task shape varies run to run), ratcheting is the wrong model — report the
rate and stop.

---

### Item 6 — Live-model accumulation benchmark

**Upstream status:** the three-regime suite and `drip_exfil_over_writes` exist
with a deterministic mock result (4/4 → 2/4 → 0/4). The separation thesis
explicitly rests on transcript-conditioned gates being *unreliable* at
accumulation — a claim a reviewer will test with a live model.

**Change.** Run the existing suite (esp. the drip task) against 2–3 live
models via the OpenRouter client; report per-regime ASR with model+version
pinned in `benchmarks/results.md`. No new harness — the suite exists; this is
the number.

**Acceptance.** Results table with live-model ASR for all three regimes;
negative or null results published as-is (the 0.3.0 precedent).

**Kill.** If live models refuse the injection unaided across the board (ASR 0
undefended), publish that and freeze — do not manufacture a delta. (Exactly
the honest outcome 0.3.0 already set precedent for.)

---

### Item 7 — Small gaps

- **Client auth-failure fallback.** `client_fallback: openrouter` in schedule
  YAML: when the primary client fails auth/availability (e.g. Copilot token
  revoked — a real ToS-adjacent risk for headless automation), the run retries
  once on the fallback client and the umpire summary + scout_hook name the
  failover. Distinct from `degrade_to` (cost-triggered) and `headless_fallback`
  (best-of-K). *Acceptance:* kill the primary client in a fixture → run
  completes on fallback, failover surfaced.
- **Workspace taint in scout_hook.** The scout event doesn't surface TaintStore
  state; add `taint: {has_any, sources_count}` so Scout can quarantine
  summaries from tainted workspaces without opening the transcript.
  *Acceptance:* tainted-workspace run emits the block; clean one emits
  `has_any: false`.

---

## Dependency order

```
Item 0 (conformance) ──── independent; Item 3's round-trip test plugs into it
Item 1 (receipts) ──┬──> Item 2 (gh tools embed receipts)
                    └──> Item 3 (plugin verdicts become receipt-compatible)
Item 4 (gateway) — after 0 (conformance fixtures reused) and 1 (receipts)
Items 5, 6, 7 — independent
```

## One-line summary for the implementer

The contract-as-vocabulary move is made; v3 makes it load-bearing: prove the
implementations agree (conformance), bind every verdict to the exact policy it
graded (receipts), route agent PRs through typed commit tools that carry those
receipts, and let the plugin declare honestly which fields it enforces — then
grow adopters (gateway) and sharpen the evidence (live-model benchmark).
