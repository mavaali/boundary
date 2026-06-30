# Changelog

All notable changes to Boundary are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/). Pre-1.0: minor versions may include breaking
changes. 1.0 is reserved for the envelope closing the full lethal trifecta
(information-flow / taint) with a frozen API.

## [Unreleased]

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
