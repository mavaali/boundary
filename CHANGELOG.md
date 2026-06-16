# Changelog

All notable changes to Boundary are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/). Pre-1.0: minor versions may include breaking
changes. 1.0 is reserved for the envelope closing the full lethal trifecta
(information-flow / taint) with a frozen API.

## [Unreleased]

### Added
- **Taint / provenance dimension (`--on-taint {refuse,warn,allow}`)** — closes the
  write-as-exfil channel (the lethal trifecta's third leg). Reading untrusted
  external content (`fetch_url`) marks the run tainted; a subsequent write to a
  writable sink trips a `taint_flow` event. `warn` (default) records it, `refuse`
  blocks the write, `allow` disables the check (surfaced as a downgrade). Coarse,
  run-level; workspace-only runs never trip it. Third Umpire emits a `taint_flow`
  verdict line; `stage_proposal` records the taint set; `on_taint:` works in
  schedule YAML. The selftest `taint_flow_enforced` guarantee is now enforced.

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

[0.2.0]: https://github.com/mavaali/boundary/releases/tag/v0.2.0
[0.1.0]: https://github.com/mavaali/boundary/releases/tag/v0.1.0
