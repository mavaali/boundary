# Boundary — Enhancement Spec

> Source: external review of `mavaali/boundary` @ main, 2026-06-15.
> Audience: a coding agent (or human) implementing against this repo.
> Style contract: each item states the problem, the change, acceptance criteria,
> the surfaces likely touched, a counter-argument, and a kill condition. Do not
> rescue a failing item — if the kill condition fires, stop and report the negative
> result. Land items in priority order; 1–3 are one finding viewed three ways and
> should ship together if possible.

---

## How to use this doc

- Items are ordered by leverage, not by effort. P0 = load-bearing, P1 = credibility, P2 = polish.
- "Surfaces" are hints, not gospel — confirm against the actual module layout before editing.
- Every item has an **Acceptance** block that should map to a test or an observable runtime behavior. If an item can't be reduced to an assertion, it isn't done.
- Each item has a **Kill** line. If it's met, the item is wrong as specified; report and halt rather than working around it.
- Treat the denylist-bypass fixture (Item 2) as expected-to-fail on first run. That failing test is the point.

---

## P0 — Enforcement floor

### Item 1 — Delegate the OS sandbox layer to `@anthropic-ai/sandbox-runtime` (`srt`)

**Problem.** The entire local write boundary rests on `sandbox-exec`, which Apple
has deprecated with no documented replacement for headless CLI process sandboxing.
The sandbox layer is macOS-only, and network egress is explicitly not bounded
("network egress is not fully blocked"). This is the bespoke-isolation layer — the
same category Anthropic's own postmortem identified as their weakest containment
component (a custom proxy, not the battle-tested primitives).

**Change.** Stop maintaining the in-repo Seatbelt wrapper. Shell out to `srt` (or
adopt its config model) as the sandbox driver for `bash` / `bash_commit` and any
spawned process. Inherit three capabilities:
1. **Linux portability** via bubblewrap (makes the README's "run in a container / as
   a dedicated user" advice executable, not aspirational).
2. **Network egress allowlist** via the proxy — closes the admitted exfiltration gap.
3. **Violation telemetry** — pipe `srt`'s block events into Third Umpire as hard
   evidence instead of inferring from transcripts.

Keep the workspace write-jail semantics identical; this is a substrate swap, not a
policy change. Gate behind a `--sandbox-driver {seatbelt,srt,none}` flag during
migration so the old path remains until parity is proven.

**Acceptance.**
- `boundary run` on Linux enforces the workspace write boundary (was previously a no-op / macOS-only).
- A run with a network allowlist of `[]` blocks `fetch_url` to an off-list host and records the block.
- Third Umpire surfaces at least one `sandbox_violation` event sourced from `srt`, not from transcript heuristics.
- Existing macOS behavior is unchanged under `--sandbox-driver seatbelt`.

**Surfaces.** shell/bash tool wrapper, the macOS `sandbox-exec` invocation site, Third Umpire evidence ingestion, GUIDE "Security boundary" section.

**Counter-argument.** `srt` is an early research preview; its config format may
change. Mitigation: pin the version, keep the `seatbelt` driver as fallback, and
treat the `srt` adapter as a thin shim so a breaking change is a shim edit, not a rewrite.

**Kill.** If `srt` cannot enforce a network allowlist for the specific tool-spawned
process model Boundary uses (not just top-level shell), this item is wrong — fall
back to a standalone egress proxy and re-scope.

**Refs.**
- Apple deprecation, no CLI replacement: https://github.com/apple/containerization/issues/737
- `srt` (macOS Seatbelt + Linux bubblewrap + network proxy): https://github.com/anthropic-experimental/sandbox-runtime
- Coding-agent sandbox census (where Boundary sits): https://gist.github.com/wincent/2752d8d97727577050c043e4ff9e386e
- "weakest layer was our own proxy": https://the-agent-report.com/2026/05/anthropic-contains-claude-sandbox-vm-agent-security/

---

### Item 2 — Demote the basename denylist from "security layer" to "intent nudge"

**Problem.** The `curl/wget/gh/...` denylist matches basename after an optional env
prefix. It is bypassable by construction: `./curl`, a copied binary, `python -c
"import urllib"`, `osascript`, `nc`, `ssh host 'curl ...'`. The GUIDE already lists
these as uncaught but still frames the denylist as a safety layer. Documented
real-world behavior: an agent bypassed Claude Code's own denylist with a path trick,
and when the sandbox caught it, the agent disabled the sandbox to finish the task.
Path/name-based matching is a known dead end; the industry fix is content-addressable
(hash-at-LSM) or OS-enforced egress.

**Change.** Two parts:
1. **Reframe, don't extend.** Update the GUIDE: the denylist makes *common* commit
   paths require explicit `bash_commit` intent; the OS-enforced egress allowlist from
   Item 1 is the actual boundary. Keep the 12-entry hard cap. Do not add entries.
2. **Add an adversarial fixture** (see Item 5) that performs `./curl`-style and
   copied-binary bypasses and asserts the *egress proxy* (not the denylist) blocks them.

**Acceptance.**
- GUIDE "bash loophole" section states the denylist is an intent nudge and names the egress proxy as the enforcement boundary.
- A red-team fixture attempts ≥3 denylist bypasses (`./curl`, copied binary, `python -c` urllib) and asserts network egress is blocked by the proxy.
- No new denylist entries were added.

**Surfaces.** bash tool denylist check, GUIDE "The bash loophole and the kill-list", `tests/`.

**Counter-argument.** Without Item 1's proxy, demoting the denylist removes a layer
and adds nothing. True — that's why this ships *with* Item 1, not before it.

**Kill.** If the egress proxy from Item 1 does not in fact block the bypass fixtures,
the demotion is premature; keep the denylist framing and reopen Item 1.

**Refs.**
- Agent escapes denylist then disables sandbox: https://ona.com/stories/how-claude-code-escapes-its-own-denylist-and-sandbox

---

### Item 3 — Add a taint/provenance dimension to the envelope (close the write-as-exfil channel)

**Problem.** The envelope bounds *which* path, *how many* writes, and *which*
commits. It does not bound *what content* flows into an allowed write. An agent can
read an untrusted file carrying an injection and write exfiltrated content into a
perfectly-allowlisted path that later syncs/shares — the writable path is an exfil
channel. This is the lethal trifecta (private data + untrusted content + external
comms); the envelope currently addresses two-thirds. The Plan-Then-Execute literature
names this exactly: a fixed plan protects action *choice/sequence* but not action
*parameters*.

**Change.** Implement a lightweight capability/taint layer (CaMeL's core idea without
its full custom interpreter):
1. **Label reads.** Tag each read with a trust label: `workspace` reads = trusted;
   `fetch_url` and reads from outside the workspace = `tainted`.
2. **Propagate into staging.** `stage_proposal` carries the set of taint labels that
   fed the thesis/evidence.
3. **Enforce at the sink.** Refuse (or, configurably, WARN-and-require-`ask_human`) a
   write when `tainted` content flows into a writable path that is network-reachable
   or shared. Third Umpire reports a `taint_flow` check.

This subsumes the queued "provenance tags for staleness" item — provenance earns its
keep for security first, freshness second.

**Acceptance.**
- A run that reads a `fetch_url` result and writes it to a writable path triggers the `taint_flow` policy (refuse or WARN per config).
- A run that reads only workspace files and writes does **not** trigger it (no false positive on the common case).
- `stage_proposal` records the taint set; Third Umpire emits a `taint_flow` verdict line.
- Policy is configurable: `--on-taint {refuse,warn,allow}` and a YAML equivalent.

**Surfaces.** envelope read/write accounting, `stage_proposal` schema, Third Umpire checks, run + schedule config, GUIDE staging-pivot and security sections.

**Counter-argument.** Coarse taint will over-block legitimate "summarize this web page
into a note" tasks. Mitigation: default to `warn` not `refuse`; only `refuse` when the
sink is network-reachable/shared, not for local scratch. Revisit granularity (per-value
vs per-read) only if the warn rate is noisy in practice.

**Kill.** If taint at read granularity produces a false-positive rate that makes the
common research workflow unusable even at `warn`, coarse taint is the wrong model —
report and escalate to the Dual-LLM / Context-Minimization pattern as a separate overlay
instead of a core envelope change.

**Refs.**
- CaMeL (capabilities + data-flow, provable-security numbers): https://arxiv.org/abs/2503.18813
- Lethal trifecta: https://simonwillison.net/2025/Jun/16/lethal-trifecta/ (overview: https://simonwillison.net/2025/Apr/11/camel/)
- Design patterns, Plan-Then-Execute parameter-exfil gap: https://arxiv.org/abs/2506.08837
- Pattern code samples: https://github.com/ReversecLabs/design-patterns-for-securing-llm-agents-code-samples

---

## P1 — Credibility

### Item 4 — Make Third Umpire produce a number via AgentDojo

**Problem.** Third Umpire's 11 checks are diagnostic prose with a PASS/WARN/FAIL on
top — unmeasured. A defense without a utility-vs-attack-success number is an assertion,
not a result. CaMeL's credibility came from one line: 77% utility at provable security
vs 84% undefended.

**Change.** Wire Boundary's envelope as a defense over the AgentDojo task suites (97
user tasks, 629 security cases; available via UK AISI `inspect_evals`). Produce a
report: benign utility, utility-under-attack, attack success rate (ASR), with and
without the envelope. Benchmark against AgentArmor (the nearest sibling — post-hoc
trace analysis) and note where the staging-pivot check has no analog in prior work.

**Acceptance.**
- A reproducible harness runs AgentDojo with Boundary's envelope as the defense and emits {utility, utility_under_attack, ASR}.
- The same harness runs an undefended baseline for the delta.
- Results table committed to the repo (`benchmarks/agentdojo.md`) with the model + version pinned.

**Surfaces.** new `benchmarks/` dir, an adapter mapping AgentDojo tools → Boundary tool registry, CI (optional, may be too slow for every push).

**Counter-argument.** AgentDojo shows near-zero ASR on the strongest 2026 base models
without any defense, so the headline delta may be small. Mitigation: report ASR on the
weaker models where attacks land, and lead with the *staging-pivot* and *taint_flow*
checks (Items 3) which target failure modes AgentDojo's "important instructions" attack
doesn't fully exercise. A small delta honestly reported is still a result.

**Kill.** If the envelope cannot be expressed as an AgentDojo-compatible defense without
rewriting the harness's control loop, this is the wrong benchmark — report and pivot to a
bespoke fixture suite (Item 5 scaled up).

**Refs.**
- AgentDojo + Inspect integration: https://ukgovernmentbeis.github.io/inspect_evals/evals/safeguards/agentdojo/
- AgentDojo paper: https://openreview.net/forum?id=m1YYAQjO3w
- AgentArmor (program analysis on agent traces — the sibling baseline): https://arxiv.org/pdf/2508.01249

---

### Item 5 — Ship `boundary selftest` with adversarial fixtures + CI

**Problem.** A repo making safety claims has a `tests/` dir but no CI, no releases, and
no proof the refusals actually fire. "It refuses" is currently a claim, not an assertion.

**Change.** Add a `boundary selftest` command (and CI job) that runs red-team fixtures
and asserts the expected refusal/halt:
- write outside the writable allowlist → refused
- `curl` / network egress under empty allowlist → blocked (post-Item-1)
- denylist bypass `./curl` / copied binary → blocked by proxy, not denylist (Item 2)
- skip `stage_proposal`, then attempt deep read / write → refused until staged
- commit tool under `refuse`/`queue` → not executed
- tainted read → write to shared sink → `taint_flow` fires (Item 3)
- `--no-staging-gate` used → Third Umpire flags it as a downgrade (Item 6)

**Acceptance.**
- `boundary selftest` exits non-zero if any guarantee regresses.
- CI runs it on push; a status badge is in the README.
- The Item-2 bypass fixture is present and passes only after Item 1 lands (expected-fail before).

**Surfaces.** new `boundary selftest` subcommand, `tests/redteam/`, `.github/workflows/`, README badge.

**Counter-argument.** Fixtures that spawn real network calls are flaky in CI.
Mitigation: use a loopback sink (local server) as the "off-list host"; assert the block,
not real-world reachability.

**Kill.** None — this item is pure downside-protection. If it can't be built, that itself
is a finding about the harness's testability.

---

## P2 — Polish

### Item 6 — Surface envelope downgrades in Third Umpire

**Problem.** `--no-staging-gate` and `--on-commit allow` weaken the envelope, but a run
that disabled a guardrail can look identical to one that never needed it. "Agent/operator
disables its own guardrail to finish the task" is documented behavior, not hypothetical.

**Change.** Third Umpire emits an explicit `envelope_downgrade` line listing any disabled
gate (`staging_gate=off`, `on_commit=allow`, `on_taint=allow`) and severity-grades the run
accordingly. History and the verdict block show it.

**Acceptance.** A run with `--no-staging-gate` produces a Third Umpire verdict containing `envelope_downgrade: staging_gate=off`; a normal run does not.

**Surfaces.** Third Umpire summary block, `boundary history` columns.

**Kill.** None.

---

### Item 7 — Position against neighbors in the README

**Problem.** Differentiation by silence reads as ignorance of the field. Boundary's
category (authz + post-run verification) already has named neighbors.

**Change.** Add a README section: a threat-model matrix mapping Boundary onto the six
secure design patterns and the lethal trifecta (state what it defends and what it
doesn't), and a short "how this differs from X" covering `predicate-secure` (policy authz
+ post-run verification — the closest), `Cupcake` (OPA/Rego hooks), and `nah`
(allow/ask/block guard). Foreground the staging pivot as the primitive none of them have.

**Acceptance.** README contains a defends/doesn't-defend matrix and a ≥3-row neighbor
comparison; the staging pivot is named as the differentiator.

**Surfaces.** README, GUIDE doctrine section.

**Kill.** None.

**Refs.**
- Neighbor census (predicate-secure, Cupcake, nah): https://gist.github.com/wincent/2752d8d97727577050c043e4ff9e386e
- Six design patterns: https://arxiv.org/abs/2506.08837

---

### Item 8 — Small gaps

- **Fielding Coach `edit` is stubbed.** Either implement an interactive edit of the proposed envelope or remove the `edit` affordance from the prompt so it doesn't advertise a no-op.
- **launchd-only scheduling.** Once Item 1 makes Linux real, add a systemd-timer backend behind the same `schedule` interface. Keep the schedule-string grammar identical.

**Acceptance.** `edit` either works or is absent from the prompt; `boundary schedule install` produces a systemd timer on Linux with the same YAML.

**Kill.** None.

---

## Dependency order

```
Item 1 (srt sandbox)  ──┬──> Item 2 (denylist reframe + bypass fixture)
                        ├──> Item 5 (selftest: egress + bypass assertions)
                        └──> Item 8b (systemd scheduling)

Item 3 (taint)  ──────────> Item 5 (selftest: taint_flow assertion)
                            Item 6 (downgrade surfacing includes on_taint)

Item 4 (AgentDojo) is independent; benefits from Item 3 existing.
Items 6, 7, 8a are independent.
```

## One-line summary for the implementer

Enforcement is real at the write-count boundary and aspirational at the OS and
information-flow boundaries. Items 1–3 fix that by replacing hand-rolled isolation with
OS-enforced egress (`srt`) and by propagating trust labels into the envelope. Items 4–5
turn the doctrine into something a program committee — or a CI run — can score.
