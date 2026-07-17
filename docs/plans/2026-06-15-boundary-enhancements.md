# Boundary Enhancement Spec — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn `enhancements.md` (8 items, P0→P2) into a sequenced, surface-accurate build plan grounded in the actual `boundary/` module layout.

**Architecture:** Boundary is a Python CLI. An `Envelope` (boundary/envelope.py) wraps a tool registry and enforces write/commit/staging bounds at call time; `ThirdUmpire` (boundary/third_umpire.py) post-grades a transcript against 12 checks. The OS write-jail is a `sandbox-exec` profile in boundary/tools/shell.py (macOS-only, no egress control). This plan adds OS-enforced egress, an information-flow taint dimension, a benchmark number, and a CI-backed selftest.

**Tech stack:** Python 3, argparse CLI, pytest, PyYAML, subprocess. New deps under consideration: `@anthropic-ai/sandbox-runtime` (`srt`) as an external binary; `inspect_evals` (AgentDojo) for Item 4.

**Platform support policy (revised 2026-06-16 after the srt spike):** macOS (Seatbelt) and Linux (bubblewrap) are first-class via `srt`. **CORRECTION to the earlier policy:** the spike found `srt` ships a **native Windows backend** (WFP filters — `srt windows-install`), so native Windows egress enforcement is available through the *same* tool, not a bespoke effort. The earlier "native Windows ships `--sandbox-driver none`, no native driver in scope" recommendation was wrong on the facts. Windows is therefore supported two ways: native (srt/WFP, one-time `srt windows-install` UAC step) or via WSL2 (the Linux/bubblewrap path). The `none` driver remains only as an explicit opt-out, not the Windows story.

---

## Surface reality-check (verified against the repo, 2026-06-15)

The spec's "Surfaces" hints are mostly right but a few names differ. Confirmed:

| Spec reference | Actual location |
|---|---|
| "bash tool wrapper / sandbox-exec site" | `boundary/tools/shell.py:28` `_run_workspace_bash`; Linux no-op at `:30-31`; profile at `:17-25` |
| "basename denylist" | `BASH_COMMIT_DENYLIST` docstring `boundary/envelope.py:35-46`; enforcement near `:315-325`; Third Umpire surfacing `boundary/third_umpire.py:359` (`bash_egress_denylist`) |
| "envelope read/write accounting + stage_proposal" | `boundary/envelope.py` — `EnvelopeEvent.kind` taxonomy `:166`, staging gate `:209-235`, `_stage_proposal_tool` `:463-501` |
| "Third Umpire checks" | `boundary/third_umpire.py` — `CheckResult` `:33`, `ThirdUmpireReport.verdict` `:46`, 12 checks ending `:372` |
| "tool registry" | `boundary/tools/registry.py` — `ToolKind = read\|write\|external\|commit` `:5` |
| taint sources | `fetch_url` `boundary/tools/web.py:11` (kind=external), `read_file` `boundary/tools/fs.py:13` (kind=read) |
| "`boundary run`" | **No such subcommand.** Entry points are `fielding-coach` (NL→envelope), `schedule-run`, `headless`. The selftest/docs must use real command names. (The `stark`/`fury` aliases were removed in the Avenger cleanup.) |
| "schedule (launchd-only)" | `boundary/schedule.py` + `boundary/launchd.py`; CLI `schedule install/uninstall/list/validate` `boundary/cli.py:70-78` |

**Decision gates flagged inline** (`⛔ DECISION`) are points where I need a call from Mihir before that item proceeds.

---

## Recommended sequencing

The spec's dependency graph says Item 1 unblocks 2/5/8b, and 1–3 should ship together. But Item 1 rides an external research-preview (`srt`) with real kill-risk, so I do **not** want it as the first keystroke. Recommended order:

1. **Item 5 (selftest harness) — current envelope first.** Pure downside-protection, zero external dependency, gives a regression net everything else leans on. Land the Item-2 bypass fixture as **expected-fail** now (the spec explicitly wants this).
2. **Items 6, 7, 8a (polish).** Self-contained, no architectural risk, shippable in a day. Good momentum + they make the doctrine legible.
3. **Item 1 (srt sandbox).** The keystone. Behind `--sandbox-driver {seatbelt,srt,none}`. Only after a spike proves `srt` can bound the *tool-spawned* process egress (the kill condition).
4. **Item 2 (denylist reframe).** Flips the expected-fail fixture from step 1 to passing. Ships with Item 1.
5. **Item 3 (taint layer).** Independent of 1; large; new info-flow subsystem.
6. **Item 4 (AgentDojo number).** Independent; benefits from 3 existing; slowest.
7. **Item 8b (systemd scheduling).** Needs Item 1's Linux reality first.

---

## Item 5 — `boundary selftest` + adversarial fixtures + CI  *(✅ DONE 2026-06-15)*

**Shipped.** `boundary/selftest.py` holds one `check_*` per guarantee backing both the `boundary selftest` CLI (exit non-zero on regression) and `tests/redteam/` (CI). 3 enforced guarantees pass today (write boundary, staging gate, commit refuse); 4 gated checks (egress/Item 1, denylist-bypass/Item 1-2, taint/Item 3, downgrade/Item 6) are present as `expected_fail` + pytest `xfail`, flipping to PASS automatically when their item lands. CI: `.github/workflows/selftest.yml` runs `pytest -q` + `boundary selftest` on push/PR; README badge added. Suite: 71 passed, 4 xfailed.

**Why first:** No dependency, no kill condition (spec: "pure downside-protection"). Establishes the assertion harness the security items prove themselves against.

**Files:**
- Create: `boundary/selftest.py` (fixture runner + assertions)
- Create: `tests/redteam/__init__.py`, `tests/redteam/test_write_boundary.py`, `tests/redteam/test_staging_gate.py`, `tests/redteam/test_commit_policy.py`, `tests/redteam/test_denylist_bypass.py` *(expected-fail until Item 2)*
- Modify: `boundary/cli.py` — add `selftest` subparser (near `:46` subparser block) + dispatch
- Create: `.github/workflows/selftest.yml`
- Modify: `README.md` — status badge

**Step 1: Write the failing test for the write boundary.**
Construct an `Envelope` with `writable_paths=["out/"]`, run a wrapped `write_file` to `../escape.txt`, assert the event is `write_refused`. (Pattern: mirror existing `tests/test_envelope_writes.py`.)

**Step 2: Run it, confirm it fails** (`selftest` module/command not present yet).
Run: `pytest tests/redteam/test_write_boundary.py -v` → FAIL (ImportError).

**Step 3: Implement minimal `boundary/selftest.py`** exposing `run_selftest() -> int` that executes each fixture and returns non-zero if any guarantee regresses.

**Step 4: Wire the CLI.** Add `sub.add_parser("selftest", ...)` and dispatch to `run_selftest()`, `sys.exit(code)`.

**Step 5: Run, confirm pass.** `python -m boundary.cli selftest` → exit 0; regress a bound locally → exit non-zero.

**Step 6: Add the remaining fixtures** (one test = one guarantee from the spec's bullet list):
- write outside allowlist → refused ✅ now
- skip `stage_proposal` then deep-read/write → refused until staged ✅ now (gate at envelope.py:209)
- commit tool under `refuse`/`queue` → not executed ✅ now
- `curl`/egress under empty allowlist → blocked — **marker `xfail(reason="needs Item 1 egress proxy")`**
- `./curl` / copied-binary bypass → blocked by proxy not denylist — **`xfail` (Item 2)**
- tainted read→shared-sink → `taint_flow` fires — **`xfail` (Item 3)**
- `--no-staging-gate` → flagged as downgrade — **`xfail` (Item 6)** *(or sequence Item 6 before this fixture)*

**Step 7: CI workflow.** `.github/workflows/selftest.yml` runs `pytest tests/` + `boundary selftest` on push. Use a loopback sink (local `http.server`) as the "off-list host" so no real network in CI.

**Step 8: README badge + commit.**

**Acceptance (from spec):** `boundary selftest` exits non-zero on any regression; CI runs on push; README badge present; Item-2 bypass fixture present and `xfail` until Item 1 lands.

✅ **DECISION 5a (resolved):** GitHub Actions. Repo has no `.github/` yet — this item creates it.

---

## Item 6 — Surface envelope downgrades in Third Umpire  *(✅ DONE 2026-06-15)*

**Shipped.** `downgrade_tags(require_staging, on_commit, on_taint)` in `third_umpire.py` is the single source of truth, used by both the new `envelope_downgrade` Third Umpire check (severity warn, names each disabled gate) and the `boundary history` display (derived from the stored summary JSON — no schema change). The `--no-staging-gate` flag already existed and plumbs `require_staging=False`, so a real run surfaces `staging_gate=off`. This promoted the selftest `downgrade_surfaced` guarantee from gated/xfail → enforced. Tests: `tests/test_third_umpire_downgrade.py` + `tests/redteam/test_downgrade_surfaced.py`.



**Files:**
- Modify: `boundary/third_umpire.py` — new check `envelope_downgrade` (add to the 12-check sequence before `return report` at `:372`); reads `report.summary` for `staging_gate`, `on_commit`, `on_taint`
- Modify: `boundary/envelope.py` — ensure the run summary emits `require_staging`, `on_commit`, (later) `on_taint` into the transcript end-event (`on_commit` already in summary per third_umpire.py:322)
- Modify: `boundary/history.py` — add a downgrade column
- Test: `tests/test_third_umpire_downgrade.py`

**Step 1:** Failing test — grade a transcript whose summary has `require_staging=false`; assert a `CheckResult(name="envelope_downgrade", severity="warn")` with detail containing `staging_gate=off`.
**Step 2:** Run → fail. **Step 3:** Implement the check: collect disabled gates (`staging_gate=off` if not require_staging; `on_commit=allow`; `on_taint=allow`), emit one `envelope_downgrade` line, severity `warn` if any. **Step 4:** Run → pass. **Step 5:** Normal-run test asserts the line is absent. **Step 6:** history column + commit.

**Acceptance:** `--no-staging-gate` run → verdict contains `envelope_downgrade: staging_gate=off`; normal run does not.

*Note:* a `--no-staging-gate` flag may not exist yet on `fielding-coach`/`schedule-run` (only `require_staging` field). Add the flag as part of this item so the fixture has a real surface.

---

## Item 7 — Position against neighbors in README  *(✅ DONE 2026-06-15)*

**Shipped.** New "Where Boundary sits" section in `README.md`: a defends/doesn't-defend matrix over the lethal trifecta (candidly naming the undefended legs), a mapping onto the six secure-agent design patterns (Plan-Then-Execute + Action-Selector, with the post-run Third Umpire as the twist), and a 3-row neighbor comparison (predicate-secure, Cupcake, nah) with the staging pivot named as the differentiator. Neighbor characterizations attributed to the census link. GUIDE.md "Security boundary" cross-links to it. Docs-only, no code change.



**Files:** Modify `README.md` (defends/doesn't-defend matrix + ≥3-row neighbor comparison: `predicate-secure`, `Cupcake`, `nah`); cross-link `GUIDE.md` doctrine section.

No tests — acceptance is structural: matrix present, ≥3 neighbor rows, staging pivot named as the differentiator. Single commit. Draft for Mihir's review before committing (writing-on-his-behalf → matches his voice prefs).

---

## Item 8a — Fielding Coach `edit` stub  *(✅ DONE 2026-06-15)*

**Resolved: removed.** The `edit` affordance lived in the dispatch prompt (`boundary/cli.py`, the `[y/N/edit]` input), not in `headless.py:107`. Decision was to remove the no-op rather than implement interactive edit. Done as part of the Avenger-cleanup pass: the prompt is now `[y/N]`, the dead `edit not implemented` branch is gone, and the Fielding Coach docstring/system prompt no longer advertise editing (re-prompt is the revision path).

---

## Item 1 — Delegate OS sandbox to `srt`  *(✅ CORE DONE 2026-06-16 — spike PASSED)*

**Shipped (core).** Spike confirmed srt bounds egress across the whole process tree (kill condition did not fire). `boundary/tools/sandbox.py` provides `run_sandboxed()` with `seatbelt | srt | none` drivers; `--sandbox-driver` + `--egress-allow` flags; threaded through `Agent`. The egress + denylist-bypass selftest checks flipped gated → enforced (real srt driver, loopback sink). PR #4. **Deferred follow-ups:** Third Umpire `sandbox_violation` ingestion (its own later item — couples to srt's research-preview debug stream). **Windows:** native via srt/WFP (see corrected platform policy above).



**Spike before building.** The kill condition is specific: if `srt` cannot enforce a network allowlist for Boundary's *tool-spawned process model* (not just a top-level shell), the item is wrong → fall back to a standalone egress proxy and re-scope.

**Phase 0 — spike (no production code):**
- Install `srt`, wrap a process that itself spawns a child making a `fetch`, confirm the child's egress is bounded by an allowlist of `[]`.
- ⛔ **DECISION 1a:** spike PASS → proceed; FAIL → STOP, report negative result, open the egress-proxy fallback as a re-scoped item. **Do not rescue.**

**Phase 1 — adapter behind a flag (only if spike passes):**
- Create: `boundary/tools/sandbox/__init__.py`, `boundary/tools/sandbox/seatbelt.py` (extract current `_sandbox_profile`/`_run_workspace_bash` from `shell.py`), `boundary/tools/sandbox/srt.py` (thin shim shelling to `srt`), `boundary/tools/sandbox/null.py`.
- Modify: `boundary/tools/shell.py` — `_run_workspace_bash` dispatches on driver; keep seatbelt semantics byte-identical.
- Modify: `boundary/cli.py` — `--sandbox-driver {seatbelt,srt,none}` (default `seatbelt` during migration).
- Modify: `boundary/third_umpire.py` — ingest `srt` block events as `sandbox_violation` checks (real evidence, not transcript heuristic).
- Pin the `srt` version; treat the adapter as a shim.
- **Windows:** native Windows resolves to `--sandbox-driver none` and prints a loud "no OS egress boundary" warning (per platform-support policy). WSL2 is the supported Windows path and uses the `srt`/bubblewrap Linux driver unchanged — no Windows-specific code here. A native Windows sandbox driver is explicitly out of scope for this item.

**Acceptance:** Linux enforces the workspace write boundary (was no-op at shell.py:30-31); empty allowlist blocks `fetch_url` to off-list host and records the block; Third Umpire surfaces ≥1 `sandbox_violation` from `srt`; macOS unchanged under `--sandbox-driver seatbelt`.

This phase needs its own TDD task breakdown written **after** the spike — granularity now is premature.

---

## Item 2 — Demote denylist to intent-nudge  *(✅ DONE 2026-06-16)*

**Shipped.** GUIDE "bash loophole" section reframed: two layers with different jobs — the srt egress proxy is the enforcement boundary, the basename denylist is an intent nudge (bypassable by construction). 12-entry cap kept, no new entries. The denylist-bypass selftest fixture (python-urllib) asserts the *proxy* blocks it, not the denylist. README + GUIDE "Security boundary" updated to document the three sandbox drivers.



**Files:** Modify `GUIDE.md` ("The bash loophole and the kill-list" section) + the `bash_egress_denylist` framing at `boundary/third_umpire.py:359-370`; flip the `xfail` denylist-bypass fixtures from Item 5 to passing (now blocked by Item 1's proxy).

**Constraints (spec):** keep the 12-entry cap, add **no** new denylist entries.
**Kill:** if Item 1's proxy does not actually block the bypass fixtures, demotion is premature — keep denylist framing and reopen Item 1.

**Acceptance:** GUIDE states denylist = intent nudge, egress proxy = boundary; ≥3 bypass fixtures (`./curl`, copied binary, `python -c` urllib) assert proxy-block; no new entries.

---

## Item 3 — Taint/provenance dimension  *(✅ CORE DONE 2026-06-16 — the 1.0 gate)*

**Shipped (core).** Coarse run-level taint: `fetch_url` (external) marks the run tainted; a write/commit to a writable sink then trips a `taint_flow` event under `Envelope.on_taint` (`warn` default / `refuse` / `allow`). Workspace-only runs never trip it (no false positive — kill condition did not fire; `warn` is non-blocking). Threaded through `--on-taint` (run), `on_taint:` (schedule YAML), `stage_proposal` records the taint set, Third Umpire emits a `taint_flow` check, and `on_taint=allow` surfaces as an envelope downgrade. selftest `taint_flow_enforced` promoted gated → enforced. **3a deferred:** selective `refuse`-by-sink-type (currently `refuse` blocks all writes post-taint); per-value/per-sink granularity is future work. Tests: `tests/test_taint_flow.py`, `tests/redteam/test_taint.py`.



**Files:**
- Modify: `boundary/tools/registry.py` — carry a `trust` label on tool results (or wrap return values with a `(label, content)` envelope)
- Modify: `boundary/tools/web.py:25` (`fetch_url` → `tainted`), `boundary/tools/fs.py:22` (`read_file`: `workspace`→trusted, outside-workspace→`tainted`)
- Modify: `boundary/envelope.py` — propagate taint set into `_stage_proposal_tool` (`:463`); enforce at write sinks (`write_file`/`append_file`/`edit_file` in fs.py); new `EnvelopeEvent.kind = "taint_flow"` (extend taxonomy at `:166`)
- Modify: `boundary/third_umpire.py` — `taint_flow` check + verdict line
- Modify: `boundary/cli.py` — `--on-taint {refuse,warn,allow}` + YAML equivalent in schedule config
- Test: `tests/test_taint_flow.py`

**Default policy (spec):** `warn`, not `refuse`; only `refuse` when sink is network-reachable/shared, not local scratch.
**Kill:** if read-granularity taint produces a false-positive rate that makes the common research workflow unusable even at `warn`, coarse taint is wrong → report, escalate to Dual-LLM/Context-Minimization overlay instead of a core change. **Do not rescue.**

**Acceptance:** `fetch_url`→write triggers `taint_flow` (refuse/warn per config); workspace-only read→write does NOT (no false positive); `stage_proposal` records the taint set; Third Umpire emits a `taint_flow` verdict; `--on-taint` configurable.

⛔ **DECISION 3a:** "network-reachable / shared" sink detection — how is a writable path classified as shared? (git-tracked? synced dir? explicit config list?) This is the crux of avoiding false positives; needs a definition before build.

Full TDD task breakdown to be written after 3a is decided.

---

## Item 4 — AgentDojo number  *(independent, slowest, HAS KILL CONDITION)*

**Files:** Create `benchmarks/` (adapter mapping AgentDojo tools → Boundary registry, runner), `benchmarks/agentdojo.md` (results table, model+version pinned). Optional CI (likely too slow for every push).

**Kill:** if the envelope can't be expressed as an AgentDojo-compatible defense without rewriting the harness control loop → wrong benchmark, pivot to a scaled-up bespoke fixture suite (Item 5++).

**Acceptance:** reproducible harness emits `{utility, utility_under_attack, ASR}` with and without the envelope; baseline delta; results committed with model/version pinned.

**Counter-argument noted:** strong 2026 base models show near-zero ASR undefended → small delta likely. Lead with staging-pivot + `taint_flow` (Item 3) checks; report ASR on weaker models where attacks land. A small, honestly-reported delta is still a result.

⛔ **DECISION 4a:** which model(s) to pin for the benchmark, and is the compute budget for a 629-security-case run acceptable?

---

## Item 8b — cross-platform scheduling backends  *(systemd needs Item 1's Linux reality)*

**Files:** Create `boundary/systemd.py` (mirror `boundary/launchd.py`); create `boundary/schtasks.py` (Windows Task Scheduler backend, for native-Windows + WSL2-interop parity); modify `boundary/schedule.py` to dispatch on platform; keep the schedule-string grammar + YAML identical across all three backends.
**Acceptance:** `boundary schedule install` produces a systemd timer on Linux, and a Task Scheduler entry on native Windows, from the same YAML. Under WSL2 the Linux/systemd path is used.

*Note:* the Windows Task Scheduler backend is cheap (a third `subprocess` shell-out, like `launchd.py`) and carries no isolation risk — unlike the native Windows *sandbox*, which stays out of scope. Don't conflate the two.

---

## Open decisions blocking a clean start

1. ~~**DECISION 5a** — CI provider~~ → **RESOLVED: GitHub Actions.**
2. ~~**DECISION 8a** — Fielding Coach `edit`~~ → **RESOLVED: removed (done 2026-06-15).**
3. **DECISION 1a** — gated on the `srt` spike result (can't pre-answer).
4. **DECISION 3a** — definition of a "shared / network-reachable" write sink.
5. **DECISION 4a** — benchmark model pin + compute budget.

Items 5, 6, 7 can start immediately (their blocking decisions are resolved). 1/3/4 each need their gate resolved before their detailed task breakdown is worth writing.
