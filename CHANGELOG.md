# Changelog

All notable changes to Boundary are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/). Pre-1.0: minor versions may include breaking
changes. 1.0 is reserved for the envelope closing the full lethal trifecta
(information-flow / taint) with a frozen API.

## [Unreleased]

### Added
- **Third Umpire `thrashing` check** — typed feedback (feature A) has labelled
  every tool result `success | arg-invalid | policy-refused | runtime-error`
  since it shipped, but nothing graded the mix. A run could clear every hard gate
  and still be mostly noise: burn twelve of sixteen calls on malformed or refused
  actions, land its one write, and grade PASS. The check WARNs when the
  unproductive share of a run's classified tool results reaches 50% over at least
  5 results, and names the dominant failure class so the operator knows what to
  fix — policy-refused heavy means the envelope is mis-specified, arg-invalid
  heavy means the tool contract is unclear, runtime-error heavy means the
  environment is broken. Ratio, not count, so long healthy runs aren't punished
  for scale. Complements the in-band no-progress halt, which trips only on the
  *same* call repeated verbatim — an agent failing twelve different ways never
  trips that but is thrashing just as hard. Severity is `warn`, never `fail`: a
  thrashing run may still have produced a correct artifact, and `fail` is
  reserved for the envelope not holding. `summary` gains `results_by_class` and
  `unproductive_ratio` (additive within `boundary.third-umpire/v1`).
  Thresholds were calibrated against 95 real transcripts, not chosen by taste:
  across the 49 carrying `result_class`, the unproductive ratio tops out at
  0.273 (p95 = 0.231), so the 0.5 bar sits at 1.83x the observed healthy
  ceiling and fires on zero of them. Transcripts predating typed feedback emit
  no check at all rather than a passing one — an unmeasured run must not read
  as a clean one.
- **Run receipts (`boundary.receipt/v1`)** — a portable artifact binding the two
  things Boundary already produced separately: the policy
  (`Envelope.spec_dict()` + `spec_hash()`) and the grade
  (`boundary.third-umpire/v1`). A verdict alone says "the run was graded";
  the receipt says *graded against this exact policy*. Every scheduled run now
  emits one (new `runs.receipt_json` column, migrated on open; a
  `<transcript>.receipt.json` file; and a `receipt` block on the `scout_hook`
  event). `boundary receipt show <run-id>` prints it; `boundary receipt verify
  <run-id>` re-hashes the embedded spec and re-grades the transcript — catching
  a tampered policy, a tampered verdict, or a receipt re-pointed at a different
  run — and exits non-zero on mismatch. The runner logs the full spec + hash
  into `envelope_start` so receipts reconstruct from a transcript alone. New
  selftest guarantee `receipt_verifies` — **9 enforced, 0 gated**. Self-reported,
  not cryptographic provenance; signing can be layered on without a schema change.
  `canonical_spec_hash()` is now a shared module function so an arbitrary stored
  spec re-hashes identically to the one the run recorded.
- **Envelope spec document** — `Envelope.spec_dict()` / `spec_hash()`: the
  policy serialized as a versioned document (`spec_version: 1`, every
  enforcement-bearing dimension) with a canonical sha256. Pricing
  (`token_rates`) is excluded, so a rate-card update never changes the hash of
  what a run was *allowed to do*. This is the anchor for run receipts (a
  verdict that names the exact policy it graded against) and for non-runner
  frontends (CC plugin, MCP gateway) compiling from the same envelope.

## [0.12.0] - 2026-07-05

First release published to PyPI. The bare `boundary` name is taken by an
unrelated project, so the distribution name is **`boundary-envelope`**
(`pip install boundary-envelope`); the import package (`import boundary`) and the
`boundary` console command are unchanged.

### Added
- **Chargeback rollup** — `boundary history --by <tag>` totals spend grouped by an
  attribution tag (e.g. `--by tenant`), with `--since <days>` to window it to a
  billing period. Backed by `History.spend_by_tag()`. The read side of attribution:
  tag-scoped budgets *bound* one tenant's spend, this *reports* every tenant's — the
  bill an operator hands a client. Surfaced in `examples/` (spend-controlled loop).
- **Exportable Third Umpire verdict** — `ThirdUmpireReport.as_dict()` / `to_json()`
  emit a stable, versioned JSON document (schema `boundary.third-umpire/v1`: overall
  verdict, run summary, and every check with its severity), and `boundary
  third-umpire <transcript> --format json` prints it. This is the "evidence of
  runtime enforcement" artifact a CI gate or auditor can consume — a spec-relative
  verdict, machine-readable and pinnable. `--format markdown` remains the default.
- **Accumulation-mode benchmark task + per-call baseline** (`benchmarks/`). The
  suite now runs each task under three regimes — undefended → a per-call content
  mediator (a Progent/DLP-style baseline with no cross-call memory) → the envelope
  — and reports ASR across all three. New `drip_exfil_over_writes` task leaks the
  secret one innocuous fragment at a time across many *allowlisted* writes, so
  neither the path allowlist nor any per-call check fires; only the run-level write
  **cardinality** cap (`max_writes`) bounds it. On the deterministic mock harness
  the three-regime aggregate is **ASR undefended 4/4 → per-call 2/4 → envelope
  0/4** — the per-call baseline is structurally blind to the accumulation drip
  (and to the action-based commit), while the run-level contract closes all four.
  `InjectionTask` gains optional `max_writes`/`max_external` caps; `run_task` gains
  a `mode` (`"envelope"|"percall"|"none"`, back-compatible with `defended`).

### Changed
- **The Third Umpire now grades the write FLOOR, not just presence.** `max_writes`
  (the ceiling) is hard-enforced at the tool layer, but `min_writes` (the liveness
  floor — "enough must happen") was only ever a soft in-loop nudge; the post-run
  `produced_output` check passed on any single write. It now fails when
  `writes_executed < min_writes`, so the verdict — not just the nudge — holds a run
  to the floor its envelope declared. `envelope_start` now records `min_writes` for
  the grader; transcripts predating this field default to a floor of 1, reproducing
  the previous `> 0` behaviour exactly (backward compatible). This closes the
  safety/liveness asymmetry: both the ceiling and the floor of the write-cardinality
  contract are now enforced, one at the tool layer and one at the verdict layer.

### Fixed
- **`boundary run --client openrouter` now works** — the OpenRouter client was
  implemented and routable via `make_client` (schedules/pipelines), but omitted
  from the `run` subcommand's `--client` choices, so ad-hoc runs couldn't select
  it. Added to the choice list.
- **Wheel build no longer references a gitignored file** — the `data-files` list
  shipped `examples/.../scratch/.gitkeep`, but `scratch/` is gitignored so the file
  never exists in a clean tree, and `uv build` / `setuptools` failed the wheel step.
  Dropped the entry (the sample workspace's scratch dir is created at run time); the
  wheel and sdist now build clean. This surfaced only now because it is the first
  actual package build.

### Documentation
- **Backends section** in README + GUIDE — documents all four clients
  (`copilot` / `anthropic` / `openrouter` / `together`), their auth and default
  models, and clarifies that the `anthropic` client uses a metered API key, not a
  Claude.ai Pro/Max subscription (there is no subscription-quota client).

## [0.11.1] - 2026-07-05

### Fixed
- **Cache writes are now priced at their premium** — a cache-creation token was
  folded into fresh input and billed at 1.0× the input rate, undercounting
  cache-heavy runs. `ChatResponse` now carries `cache_creation_input_tokens`
  (populated from the Anthropic `usage`), the rate card gains a `cache_write`
  axis (Anthropic ~1.25× input; defaults to 1.25× when absent), and
  `estimate_cost` prices reads and writes on separate axes. Read-only estimates
  are unchanged, so budgets/gradient/clamp built on the estimate now bind on an
  accurate cost basis for cached workloads.

### Added
- **Cost attribution on the interactive `boundary run` path** — envelope-mode
  interactive runs now record an `(adhoc)` row to the history ledger carrying
  `--attribution key=value` tags (repeatable), so ad-hoc spend shows in
  `boundary history` and is sliceable by attribution / tag-scoped budgets
  alongside scheduled runs. Ledger writes are best-effort (a failure prints a
  note, never crashes the run) and interactive runs remain budget-*ungated*.

## [0.11.0] - 2026-07-03

### Changed
- **Secure-by-default sandbox: new `auto` driver is now the default** (`agent`,
  `schedule`, `pipeline`, `boundary run --sandbox-driver`). `auto` prefers `srt`
  (OS-enforced egress) when installed, falls back to `seatbelt` on macOS with a
  LOUD stderr warning that egress is uncontained, and refuses where neither is
  available rather than silently dropping the jail. Explicit `--sandbox-driver
  srt` stays strict (hard-fails if srt is absent). The `Agent` resolves `auto` to
  a concrete driver at construction, so the transcript and the Third Umpire's
  `egress_uncontained` check see the driver that actually ran.

### Added
- **Degrade-to-cheaper-model** (`Envelope.degrade_to` / `degrade_at`) — once spend
  crosses `degrade_at` (a fraction of the closest-to-breach cap) the run swaps onto
  a cheaper model for the rest of the run instead of only nudging: the expensive
  model does the early reasoning, the cheap one finishes under pressure. Fires once
  (`model_degrade` event, `degraded→<model>` banner). Spend is now accounted
  per-response, so mixed-model runs price each segment at the rate that was active
  when it ran. Configurable in the schedule/pipeline `envelope:` block.
- **Cost-attribution tags** (`attribution:` YAML block) — stamp arbitrary str→str
  tags (project/purpose/tenant) on every recorded run so the ledger can be sliced
  and budgets scoped by tag. New `runs.attribution_json` column (older DBs migrate
  on open); `History.spend_since(..., tag=)`; budget `scope: tag`/`tag:<key>` sums
  per distinct tag value across workspaces; pipelines auto-stamp `step:<name>`.
- **Cross-run spend budgets** (`boundary/budget.py`, `SpendBudget`) — a `budget:`
  block in a schedule/pipeline YAML bounds the SUM of run costs over calendar
  windows (daily/weekly/monthly, calendar-reset) and a trailing rolling window,
  aggregated over the existing run-history `runs` table (no second ledger). At
  run time `run_headless` either skips a run whose window is already spent out
  (`stop_reason: skipped_budget`) or clamps its per-run `max_dollars` to the
  tightest remaining headroom, so the spend gradient/halt enforce the cross-run
  ceiling from inside the run. `scope: workspace|global`. New `History.spend_since`
  and a `boundary budget <yaml>` status command (exit 3 when exhausted).
- **Spend policy gradient** (`Envelope.spend_pressure_at`, default `(0.75, 0.9)`)
  — before the hard `budget_halt` at 100% of a spend cap, the agent is nudged
  once at each fraction of whichever of `max_input_tokens` / `max_output_tokens`
  / `max_dollars` is closest to breach, and a `spend_pressure` event is logged.
  Turns the binary kill switch into a soft landing; `()` disables. Mirrors the
  iteration `budget_pressure_at` nudge.
- **Fail-closed pricing** (`Envelope.on_unpriced_model`, default `"max_rate"`)
  — a model absent from the rate card previously estimated at `$0.00`, letting
  it slip past `max_dollars` entirely (an unpriced model was an uncapped run).
  Unknown models are now priced at a conservative upper bound so the dollar cap
  still binds; the live banner shows `rate=fallback`. `"zero"` restores the
  legacy fail-open behaviour; `"<model-id>"` borrows a known model's rate. New
  helpers `Envelope.rate_for()` / `Envelope.is_priced()`.
- **Transient-failure retry for the Anthropic and Copilot clients** — a shared
  `boundary/clients/_http.py:request_with_retry` wraps each HTTP call with bounded
  exponential backoff over retryable statuses (408/429/5xx/529) and transport
  timeouts/connection errors; a persistent error is still surfaced, never masked.
  (OpenRouter kept its existing bespoke retry.)
- **Path-collision guards** — best-of-K now refuses to run when writable paths
  can't be isolated across runs (e.g. a glob target every run would clobber:
  `multirun.validate_run_path_isolation`), and pipeline `validate()` flags
  duplicate step names.
- **Symlink-escape red-team guarantee** — `selftest.check_symlink_escape_refused`
  (and `tests/redteam/test_symlink_escape.py`) assert a workspace-internal symlink
  pointing outside the jail can't become a read or write escape, including the
  sharp case where the symlink's name is on the writable allowlist.

### Tooling
- Added a `ruff` lint gate (E/F/I/B/UP) over the package; package and tests are
  lint-clean.

## [0.10.0] - 2026-06-29

### Added
- **Declarative triggers + a results→tasks queue** (a bounded BabyAGI-style loop,
  #18) — a finished run's outcome (Third Umpire verdict, discovered items, error)
  is matched against declarative `TriggerRules`; matching rules enqueue new tasks,
  **pending / priority-ordered / human-gated** — never auto-dispatched. That gate
  is the line vs BabyAGI: the loop may *propose* its next work, not run it unasked.
  Adds a `tasks` table to `history.db` (with a `parent_run_id` causal edge,
  priority, status), `boundary/triggers.py` (`TriggerRule {on, when, action}` +
  pure `evaluate_triggers`, with `enqueue_discovered` and `enqueue_followup`
  actions), `ScheduleConfig.triggers` (headless evaluates post-run and enqueues),
  and a `boundary tasks list|ready|add|approve|done|reject` CLI.

## [0.9.1] - 2026-06-29

### Fixed
- **Windows path handling in the fabricspecs discovery source** (#17) — normalize
  scanned paths to POSIX so discovery and its path exclusions work on Windows.

## [0.9.0] - 2026-06-29

### Added
- **`write_profile` lens for the Third Umpire's `spend_pacing` check** (#16) —
  declare a run's shape (`edit` / `batch` / `synthesis`) so spend is graded on the
  right axis: `edit`/`batch` on tokens-per-write (cheap output expected),
  `synthesis` on input-grounding (read-heavy by design — large input is fine when
  it lands in the artifact rather than churning).

## [0.8.0] - 2026-06-29

### Added
- **`fabricspecs_questions` discovery source + weekly discover-to-digest schedule**
  (#15) — an owner-scoped Discover source scans `discoverable: true` specs,
  extracts unanswered `## Open Questions` (table `Status=Open` or bullets), and
  emits one task per question (generated dirs hard-excluded). An optional
  `discover:` block on `ScheduleConfig` injects the discovered questions into the
  persona's task; ships a weekly triage schedule (vision persona → digest, with
  dispatch of any individual question staying human-gated).

## [0.7.0] - 2026-06-25

ComPilot incorporation — lessons from *Agentic Auto-Scheduling: An Experimental
Study of LLM-Guided Loop Optimization* (Merouani et al., PACT 2025,
arXiv:2511.00592) ported into the envelope/loop.

### Added
- **Best-of-K multi-run selection** (`boundary/multirun.py`, `boundary run --runs K`)
  — fan out K runs into per-run templated paths, Third-Umpire-gate, a bounded
  read-only judge ranks survivors, and a mode-aware **non-blocking-for-headless**
  resolution promotes the winner (interactive blocks on close calls via the
  review-queue; headless auto-picks + files a non-blocking advisory, or defers).
  Surfaced via `run --runs K`, `fielding-coach --runs K`, and scheduled YAML
  (`runs:` + `select_margin` / `judge_model` / `headless_fallback`).
- **Typed tool-result feedback** — every tool result is classified
  `success` / `arg-invalid` / `policy-refused` / `runtime-error`, surfaced on the
  envelope banner and tallied as `results_by_class` on `EnvelopeRunResult`.
- **Pre-exec validity gate** — a call missing a schema-required field is rejected
  as `arg-invalid` before the (expensive/side-effecting) tool runs; no side
  effect, no wasted iteration. `reason` stays a policy concern.
- **No-progress halt & early-stop nudge** — identical tool calls repeated past
  `repeat_halt` halt the run (`stop_reason: no_progress_halt`); a premature stop
  under `min_writes` triggers exactly one bounded continue nudge.
- Efficiency doctrine baked into the envelope note + Fielding Coach (revise with
  `edit_file` diffs not whole-file rewrites; spend on feedback, not fat priming).

### Changed
- `Envelope` gains `repeat_warn` / `repeat_halt` / `nudge_on_early_stop` knobs;
  `EnvelopeRunResult` and the `envelope_end` transcript record gain
  `results_by_class`.

## [0.6.0] - 2026-06-20

The taint milestone — the write-as-exfil channel is now bounded across **stage and
run boundaries**, and missing OS egress containment is a loud failure. This is the
major step toward the 1.0 goal of closing the lethal trifecta: taint is now coarse,
**file-granular, and persisted**. Per-value information-flow tracking remains future
work, so this is progress toward 1.0, not 1.0 itself.

### Added
- **Persisted, file-granular taint ledger** (`boundary/taint.py`) — `TaintStore`
  records untrusted sources and tainted files per workspace under
  `$BOUNDARY_HOME/taint/<hash>.json` (default `~/.boundary`), **outside** the
  workspace so a jailed agent (and the `HOME`-repointed sandboxed bash) cannot read
  or clear it. Taint now survives pipeline-stage and separate-invocation boundaries.
- **Provenance taint** — a run becomes tainted not only by `fetch_url` but by
  `read_file`/`grep` of a file the ledger marks tainted, and by `bash` when egress
  is not OS-bounded (`--sandbox-driver` ≠ `srt`). A write executed while the run is
  tainted marks its output file tainted (cross-stage propagation). Taint is causal:
  a run that reads only untainted files is never gated, even if the workspace holds
  tainted files elsewhere.
- **`egress_uncontained` check** (Third Umpire, **fail**) — a run that handled
  untrusted content under a non-`srt` driver can no longer report green, because
  network exfil is not contained without an OS egress allowlist.
- **`taint_egress` check** (Third Umpire, **warn**) — an already-tainted run that
  fetches a host outside the egress allowlist is flagged as a possible exfil channel.
- **`boundary taint --show/--clear <workspace>`** — inspect or reset the (monotonic)
  ledger for a workspace.
- **Sandbox driver / egress in scheduled and pipeline runs** — `sandbox_driver:` and
  `egress_allow:` are now honored in schedule YAML, pipeline steps, and squad
  planning (previously hard-pinned to `seatbelt`). The `Agent` is the single source
  of truth, and both are logged in `envelope_end` for the Third Umpire.
- **Tests** — cross-stage and cross-invocation taint locks (`tests/redteam/test_taint_cross_stage.py`),
  provenance/propagation, bash-taint-unless-`srt`, tainted commit-path refusal, and
  the new umpire checks.

### Changed
- **The taint gate spans runs.** Previously taint was per-run and reset at every
  stage/process boundary, so the stage that committed was blind to what an earlier
  stage fetched. It now carries via the persisted ledger.
- **`on_taint=refuse` semantics** — a write is blocked in any run that *became*
  tainted (via fetch, tainted-file read, or non-`srt` bash), across stages — not
  only within the run that did the fetch.
- README, GUIDE, and the envelope docstring rewritten to describe the file-granular
  persisted model and its honest limits (file- not byte-granular; `bash` outputs not
  individually attributed; network exfil closed only by `srt`).

### Upgrade note
Runs that handle untrusted content under the default `seatbelt` driver will now get
a Third Umpire `egress_uncontained` **fail** — this surfaces a real gap, not a
regression. It does **not** block anything under the default `on_taint=warn`, but it
will turn affected runs' verdicts red until they move to `--sandbox-driver srt` with
a tight `--egress-allow`. Transcripts from older versions (no driver logged) are
exempt — the check is skipped.

## [0.5.0] - 2026-06-16

The cross-platform-scheduling milestone: headless schedules and pipelines now
run on Windows via Task Scheduler, matching the existing macOS launchd
support. Linux remains unsupported for headless mode (use `boundary run` or
`boundary fielding-coach` directly).

### Added
- **Windows headless scheduling** (`boundary/win_scheduler.py`) — registers
  `\boundary\io.boundary.schedule.<name>` tasks via `schtasks.exe` and tracks
  them with marker files under `~/.boundary/scheduler-tasks/`. User-scope (no
  admin elevation). Same schedule grammar as macOS: `daily HH:MM`,
  `weekly <day> HH:MM`, `every N minutes`, `hourly`. Raw cron remains rejected
  on both platforms.
- **Platform dispatcher** (`boundary/scheduler.py`) — `boundary schedule
  install`, `boundary pipeline install`, `uninstall`, and `list` now route to
  the right backend by `sys.platform`. Linux raises a clear "use Mode 1 or 2"
  error instead of silently failing.
- **Windows CI** — new `selftest-windows` job in the selftest workflow runs the
  full unit suite on `windows-latest` and verifies the `boundary` CLI starts
  cleanly.
- **Scheduler tests** — `tests/test_win_scheduler.py` (schtasks args mapping,
  install/uninstall/list with mocked subprocess) and
  `tests/test_scheduler_dispatch.py` (per-platform binding + Linux fallback).

### Changed
- **Log directory rename:** `~/.boundary/launchd-logs/` → `~/.boundary/scheduler-logs/`
  on both platforms. Existing macOS logs stay where they are; new logs go to
  the new path.
- README/GUIDE: scheduling sections now describe both backends; cron-rejection
  message is platform-neutral.
- CLI subcommand help strings: "launchd" → "OS scheduler (launchd / schtasks)".

## [0.4.0] - 2026-06-16

The packageability milestone: Boundary becomes installable as a public alpha
via `pipx install git+https://github.com/mavaali/boundary.git`. Adds
squad-planned pipelines and pipeline launchd support.

### Added
- **Squad-planned pipelines** (`boundary pipeline-run <yaml>`) — one squad
  planner runs first inside its own envelope, writes a shared plan, and is
  graded by the Third Umpire; each persona step then runs as a normal Boundary
  envelope and must cite the plan in its `stage_proposal`. Two layers of
  staging (squad-level and persona-level) without losing per-step bounds.
- **Pipeline launchd support** (`boundary pipeline install <yaml>`) — install,
  list, and uninstall pipelines as headless macOS LaunchAgents, mirroring the
  existing schedule install flow.
- **Generic pipeline example** — `examples/pipelines/squad-docs-health.yaml`
  ships with the package (`share/boundary/examples/pipelines/`) so a fresh
  install can immediately run `boundary pipeline validate <example>`.
- **Public install path in README/GUIDE** — `pipx install git+...` is now the
  documented user flow; the `.venv` setup is demoted to the contributor
  section.

### Notes
- Scout/Teams notification hooks remain a private integration (consumed via
  `notify:` in pipeline/schedule YAMLs) and are not part of the public package
  guarantees. A generic `boundary scout drain` is on the roadmap.

## [0.3.0] - 2026-06-16

The lethal-trifecta-closing milestone: information-flow taint dimension, plus a
reproducible benchmark harness with first real-model results.

### Added
- **Taint / provenance dimension (`--on-taint {refuse,warn,allow}`)** — closes
  the write-as-exfil channel (the trifecta's third leg). Reading untrusted
  external content (`fetch_url`) marks the run tainted; a subsequent write to a
  writable sink trips a `taint_flow` event. `warn` (default) records it,
  `refuse` blocks the write, `allow` disables the check (surfaced as a
  downgrade). Coarse, run-level; workspace-only runs never trip it. Third
  Umpire emits a `taint_flow` verdict line; `stage_proposal` records the taint
  set; `on_taint:` works in schedule YAML. The selftest `taint_flow_enforced`
  guarantee is now enforced — **7 enforced, 0 gated**.
- **Benchmark harness** — `python -m benchmarks.run --model <slug>` runs three
  injection tasks (forbidden write, tainted exfil, unauthorized commit) defended
  vs undefended and emits `{utility, utility_under_attack, ASR}`. After spiking
  AgentDojo and hitting its kill condition (no `defense` parameter in the
  inspect port; staging/taint not exercised), pivoted to a bespoke suite
  measuring the real `EnvelopeRunner`. Mock-verified deterministically in
  `tests/test_benchmark_harness.py` (ASR 3/3 → 0/3). First real-model results
  in `benchmarks/results.md`: both Llama-3.1-8b and Haiku-4.5 refuse these
  naive injections unaided, so the envelope's measured ASR delta is 0 on this
  attack set at this model class — see file for honest interpretation.
- **OpenRouter client** (`boundary/clients/openrouter.py`) — OpenAI-compatible,
  with retry-once on transient provider errors and 200-with-error-body handling.
- **`pytest pythonpath`** — pyproject pytest config so the top-level
  `benchmarks` package imports under strict PEP 660 editable installs (CI).

## [0.2.0] - 2026-06-16

The security-floor milestone: an assertion harness for the envelope's
guarantees, and OS-enforced network egress.

### Added
- **`boundary selftest`** — adversarial fixtures that assert the envelope's
  guarantees (write boundary, staging gate, commit refusal, downgrade
  surfacing, egress) and exit non-zero on any regression. GitHub Actions CI
  workflow + README badge.
- **Pluggable OS sandbox driver** — `--sandbox-driver {seatbelt,srt,none}` with
  `--egress-allow <domain>`. The `srt` driver
  ([Anthropic sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime))
  enforces a network egress allowlist across the whole process tree
  (macOS/Linux/Windows), closing the bash exfiltration gap.
- **Third Umpire `envelope_downgrade` check** + `boundary history` downgrade
  column — a run that disabled a guardrail (`--no-staging-gate`,
  `on_commit=allow`) is now visibly distinct from one that never needed it.
- **README "Where Boundary sits"** — a defends/doesn't-defend matrix over the
  lethal trifecta, a mapping onto the six secure-agent design patterns, and a
  neighbor comparison (predicate-secure / Cupcake / nah).

### Changed
- **BREAKING:** removed the `fury` and `stark` CLI subcommand aliases — use
  `third-umpire` and `fielding-coach`.
- Renamed internal modules to the cricket theme: `fury` → `third_umpire`,
  `stark` → `fielding_coach`; `FuryReport` → `ThirdUmpireReport`. SQLite history
  columns `fury_*` → `third_umpire_*` with automatic in-place migration of
  existing databases.
- Reframed the GUIDE "bash loophole" section: the basename denylist is an intent
  nudge (bypassable by construction); the `srt` egress proxy is the enforcement
  boundary. Denylist frozen at a 12-entry cap.

### Removed
- The no-op `edit` affordance from the Fielding Coach dispatch prompt
  (`[y/N/edit]` → `[y/N]`).

### Fixed
- Guarded the macOS-only `bash_commit` sandbox test behind `SANDBOX_AVAILABLE`
  so it skips (rather than fails) on Linux CI.

## [0.1.0]

Initial Boundary release — envelope runner, Fielding Coach planner, Third Umpire
post-run grading, headless scheduling (launchd), overlays.

[0.5.0]: https://github.com/mavaali/boundary/releases/tag/v0.5.0
[0.4.0]: https://github.com/mavaali/boundary/releases/tag/v0.4.0
[0.3.0]: https://github.com/mavaali/boundary/releases/tag/v0.3.0
[0.2.0]: https://github.com/mavaali/boundary/releases/tag/v0.2.0
[0.1.0]: https://github.com/mavaali/boundary/releases/tag/v0.1.0
