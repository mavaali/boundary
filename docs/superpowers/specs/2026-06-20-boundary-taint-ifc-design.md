# Coarse file-granular taint / information-flow tracking

**Date:** 2026-06-20
**Status:** Design — approved scope, pending spec review
**Scope chosen:** A (doc honesty) + B (cross-stage taint) + C (provenance-tracked reads) + D (close bash-fetch bypass), plus the egress-uncontained "lock" (#1 fail-check + #2 nudge event + docs). The hard `--require-egress-control` flag is **deferred** to a later change.

---

## 1. Problem

Today taint is per-run, in-memory, and triggered only by `fetch_url`:

- `tainted_reads` / `tainted_sources` live in the per-run `counters` dict (`boundary/envelope.py:606`), set only when `base.kind == "external"` (`envelope.py:453-462`). `fetch_url` is the only `external` tool (`boundary/tools/web.py:23`).
- The gate fires on write/commit/bash when `tainted_reads > 0` (`envelope.py:259-278`).
- Counters are recreated every `EnvelopeRunner.run()` (`envelope.py:606`), and the pipeline runs each stage as a fresh `run_headless` → fresh `Envelope` → fresh counters (`boundary/pipeline.py:200-204`, `boundary/headless.py:257-272`). **Taint dies at every stage boundary and every process boundary.**
- `read_file` is workspace-jailed (`boundary/tools/workspace.py:20`) so it cannot read outside the workspace — which makes the "outside-workspace" half of the taint comment (`envelope.py:124-126`) **dead/misleading**.

Consequences (both verified against code):
- **Cross-stage exfil is invisible.** Stage 1 `fetch_url`s untrusted content and writes it into the shared workspace; stage 2 `read_file`s it (untainted, fresh run) and commits/writes to a shared sink. The taint gate never fires in stage 2, even under `on_taint=refuse`. Same for two separately-scheduled single runs sharing a workspace.
- **bash bypass.** `bash: python3 -c "import urllib.request; ..."` fetches untrusted content with zero taint (`curl` is denylisted; `python` is not).
- **fetch_url-as-exfil is out of scope for taint by design** (see §6) — that is an OS-egress problem, not a tool-layer one.

## 2. Goals / Non-goals

**Goals**
- Coarse, **file-granular** provenance: track which workspace files are untrusted; persist it; propagate across reads → writes, across stages, and across separate invocations sharing a workspace.
- **Causal trigger:** a run is gated only if it *itself* performed a tainted read (external fetch, read of a tainted file, or non-srt bash) and then writes. A clean stage is never gated merely because tainted files exist.
- Close the bash-fetch bypass conservatively.
- Make missing OS egress containment **loud** (a Third Umpire `fail` check), not silent.
- Make the docs describe the real model and its limits.

**Non-goals**
- Per-value / byte-level IFC, taint laundering analysis, or data-flow through transformations. We over-approximate in the safe direction and say so.
- Preventing network exfil at the tool layer (the second-`fetch_url` channel). Containment for that is `srt`'s OS egress allowlist; we only make its absence visible.
- The hard `--require-egress-control` start-gate (deferred).

## 3. Design

### 3.1 `TaintStore` — new module `boundary/taint.py`

A persisted ledger of untrusted provenance for one workspace.

- **Location:** `$BOUNDARY_HOME/taint/<workspace-hash>.json`, where `BOUNDARY_HOME` defaults to `~/.boundary` and `<workspace-hash>` = `sha256(str(workspace_root.resolve()))[:16]`. The `BOUNDARY_HOME` override is a **decided** part of the design (not optional): tests set it to a `tmp_path` so they never touch the real `~/.boundary/`, and it is the single knob for relocating the ledger root.
  - **Why outside the workspace:** the agent is jailed to the workspace (`workspace.py`), and `bash` can write anywhere *inside* it — a workspace-local ledger would be agent-clearable (`echo '{}' > .boundary/taint.json`). `_jail_env` (`boundary/tools/sandbox.py:27-42`) repoints `HOME` into the workspace for bash, so the real `~/.boundary/` is unreachable by the jailed agent and by sandboxed bash.
- **Schema (JSON):**
  ```json
  {
    "version": 1,
    "workspace": "/abs/path",
    "sources": ["https://...", "bash:python3 -c ..."],
    "tainted_files": ["intel/raw-2026-06.md", "notes/triage.md"]
  }
  ```
  `tainted_files` holds workspace-relative POSIX paths.
- **API:**
  - `TaintStore.load(workspace_root: Path) -> TaintStore` — read ledger or empty.
  - `is_tainted(rel_or_abs_path) -> bool` — normalize to workspace-relative, membership test.
  - `mark_source(src: str) -> None` — dedup-append to `sources` (cap length, e.g. last 200).
  - `mark_file(rel_or_abs_path) -> None` — normalize, add to `tainted_files`, then `save()`.
  - `save() -> None` — atomic write (temp file + `os.replace`).
- **Path normalization:** resolve against `workspace_root`; if the path escapes the workspace, treat as not-taintable (defensive — should not happen since writes are jailed).
- **Concurrency:** v1 is last-writer-wins via atomic replace. Pipeline stages run sequentially; concurrent runs on one workspace are not a supported configuration and are documented as such.

### 3.2 Wiring in `boundary/envelope.py`

`EnvelopeRunner` loads a `TaintStore` for the run's workspace at start and passes it into `_make_enforced_tool` (alongside the existing `counters`). The run is **not** pre-tainted from the store — taint is acquired causally, on read.

Changes inside the enforced wrapper:

1. **External (fetch_url) — source + taint (B):** unchanged `tainted_reads++`; additionally `store.mark_source(url)`.
2. **Provenance read (C):** for content-returning read tools — `read_file` and `grep` — if `store.is_tainted(path)` (for `grep`, if any scanned file is tainted; v1 simplification: taint when the `grep` glob could include a tainted file — see Open Questions), increment `tainted_reads` and record a source `taint-file:<path>`. `list_dir`/`glob`/`count_matches` return names/counts only and do **not** taint.
3. **Write propagation:** when a write tool (`write_file`, `edit_file`, `append_file`) executes **successfully while the run is tainted** (`tainted_reads > 0`), call `store.mark_file(path)`. (bash-written paths are unknowable and are not individually marked — see §5.)
4. **bash (D):** when `base.name == "bash"` and the run's `sandbox_driver != "srt"`, treat the call as a tainted read: `tainted_reads++` and `store.mark_source("bash:" + cmd[:60])`. Under `srt`, bash does **not** taint (egress is OS-bounded). This is evaluated before the existing commit-denylist/write-accounting logic, so the gate sees the taint on the *same* call's subsequent write accounting and on later calls.
5. **taint_egress nudge (#2):** in the external branch, if `tainted_reads > 0` and the fetched URL's host is not in the run's `egress_allowlist`, append a `taint_egress` (warn) event. Host parsed via `urllib.parse.urlsplit`. Labeled in docs as nudge-not-containment.

The `sandbox_driver` and `egress_allowlist` used in steps 4–5 are read from the **Agent** (the single source of truth — see §3.3), passed into `_make_enforced_tool` by `EnvelopeRunner` alongside the existing `counters`/`store`.

The existing taint gate (`envelope.py:259-278`) is unchanged in structure; it now simply sees a richer `tainted_reads`.

### 3.3 Threading the sandbox driver + egress allowlist (single source of truth)

The driver/allowlist actually *enforced* live on the **Agent** today: `Agent.__init__(sandbox_driver="seatbelt", egress_allowlist=None)` builds the shell tool with them (`boundary/agent.py:29-48` → `boundary/tools/shell.py:13-25`). To avoid a second copy that can drift from what bash actually ran under (advisory from spec review), **the Agent stays the single source of truth** — do **not** add these as `Envelope` fields.

- **Ensure the Agent retains them:** if `Agent` does not already store `self.sandbox_driver` / `self.egress_allowlist` (it currently takes them as constructor params and passes them straight to the shell tool), store them as attributes so `EnvelopeRunner` can read them.
- **`EnvelopeRunner` reads from `self.agent`** and (a) passes `sandbox_driver`/`egress_allowlist` into `_make_enforced_tool` (for the D check and the taint_egress nudge, §3.2 steps 4–5) and (b) logs both into the `envelope_end` event (`envelope.py:823-848`) for the Third Umpire.

**First-class wiring work — headless & pipeline cannot select `srt` today.** This is the gap the spec review caught and it is load-bearing: the `egress_uncontained` lock (§3.4) and acceptance tests #1, #2, #6 all run through the headless/pipeline path. `ScheduleConfig` (`boundary/schedule.py:18-77`) has **no** driver/egress field, and `load_persona` (`boundary/headless.py:240-248`) builds the Agent without a driver, so every scheduled and pipeline run is hard-pinned to `seatbelt`. The plan must:
- Add `sandbox_driver: str = "seatbelt"` and `egress_allowlist: list[str]` fields to `ScheduleConfig` (dataclass + `ScheduleConfig.load` YAML parsing).
- Thread them through `load_persona` (`boundary/adapters/clawpilot.py`) → `Agent`, and through `run_headless`.
- Add pipeline-level defaulting: `PipelineConfig` defaults + per-step override, mirroring how `on_taint` is already defaulted (`boundary/pipeline.py:145`).
- `cli.py` already passes both to the `Agent` (`cli.py:534-535, 558-559`) — no change needed there beyond reading them back off the Agent in the runner.

Until this wiring exists, scheduled/pipeline taint runs would always fail `egress_uncontained`; with it, operators can opt a schedule or pipeline into `srt`.

### 3.4 Third Umpire (#1 lock) — `boundary/third_umpire.py`

- **Summary:** add `sandbox_driver`, `egress_allowlist` from `envelope_end`.
- **New check `egress_uncontained` (fail):** if the run acquired taint (`tainted_reads > 0` **or** any `taint_flow`/`taint_egress` event) **and** `sandbox_driver != "srt"`, emit a `fail`-severity check naming the risk: untrusted content was handled without an OS-enforced egress boundary, so exfil via the network is not contained. **Backward compat:** if `sandbox_driver` is absent from `envelope_end` (old transcripts), skip this check (do not false-fail).
- **New check `taint_egress` (warn):** surface any `taint_egress` events, mirroring the existing `taint_flow` check (`third_umpire.py:388-401`).

### 3.5 CLI — `boundary taint`

Minimal operator surface for the now-persistent, monotonic ledger:
- `boundary taint --show <workspace>` — print sources + tainted_files.
- `boundary taint --clear <workspace>` — delete the ledger for that workspace.

### 3.6 Docs (A)

- **`envelope.py:124-130`:** rewrite the comment — remove the dead "outside-workspace"; describe the file-granular persisted model.
- **README taint section (`README.md:69-76`):** coarse file-granular + persisted + cross-stage/invocation; the door-composition paragraph on why the second-`fetch_url` channel needs `srt`, not a smarter check; `warn` vs `refuse`; the `egress_uncontained` downgrade.
- **GUIDE taint section (`GUIDE.md:626-653`):** full model, the `egress_uncontained` fail-check, `boundary taint` usage, and the §5 limits verbatim.

## 4. Component boundaries (isolation)

- `TaintStore` is pure I/O over a JSON ledger keyed by workspace; testable with `tmp_path` and a patched home dir; no envelope/runner knowledge.
- `envelope.py` consumes `TaintStore` through its small API; it does not know the ledger's on-disk format.
- `third_umpire.py` reads only the serialized `envelope_end` event (its existing contract); it gains two checks and never imports `TaintStore`.

## 5. Honest limits (must appear in docs)

- **File-granular, not byte-level.** Reading a tainted file taints the run regardless of whether tainted bytes actually reach the output — over-approximation in the safe direction.
- **bash outputs are not individually attributable.** A non-srt `bash` taints the *run*; files it writes are not individually marked tainted (we can't parse what bash wrote).
- **Network exfil is not contained here.** The second-`fetch_url` channel (and any in-band URL/DNS/timing channel) is closed only by `srt`'s OS egress allowlist. `egress_uncontained` makes its absence loud; it does not prevent it.
- **Monotonic.** Taint only accumulates; clear with `boundary taint --clear`.
- **Concurrency.** One run per workspace at a time; concurrent runs are last-writer-wins on the ledger.

## 6. Why the second-`fetch_url` hole is not closed at this layer

A per-call check on `fetch_url` is a two-way door: the agent controls the URL, so data can leave via query, path, subdomain, header, request timing, or DNS. Any URL heuristic is "a nudge, not containment" — the same property the repo already documents for the bash denylist (`GUIDE.md:599-605`). The one-way door is OS-enforced egress restriction (`srt`). This change therefore *reveals* the gap (`egress_uncontained`) rather than pretending to close it.

## 7. Acceptance tests (the locks)

New tests under `tests/` (+ `tests/redteam/` where adversarial):

1. **`research → apply` cross-stage commit gate.** Stage 1 (`fetch_url` → `write_file` to `notes/triage.md`) then stage 2 (separate run; `read_file("notes/triage.md")` → `bash_commit`). Assert: under `warn`, a `taint_flow` event on stage 2 and a failed Third Umpire `taint_flow` check; under `refuse`, the commit is blocked (`commit_executed == 0`).
2. **`scout → synthesize` cross-invocation gate.** Stage 2 runs as a *separate* `run_headless` invocation (new counters), proving the persisted ledger — not in-memory threading — carries the taint. Same assertions as #1.
3. **Causal negative.** A stage that reads only a clean file and writes is **not** gated, even with `tainted_files` present in the ledger. (Proves we did not build the "gated forever" cliff.)
4. **Provenance read.** `read_file` of a tainted file sets `tainted_reads > 0`.
5. **bash bypass closed.** `bash` under `sandbox_driver != "srt"` taints the run; under `srt` it does not.
6. **egress_uncontained lock.** A tainted run under `seatbelt` → Third Umpire `egress_uncontained` fail; under `srt` → no such fail. Old transcript without `sandbox_driver` → check skipped (no false fail).
7. **Ledger is agent-unreachable.** A `write_file` targeting the ledger path is refused (not in `writable_paths`) and the ledger lives outside the workspace root.
8. **Regression.** Existing `tests/test_taint_flow.py` and `tests/redteam/test_taint.py` still pass (single-stage behavior unchanged), and `boundary selftest` stays green.

## 8. Backward compatibility

- New `ScheduleConfig` driver/egress fields default to current behavior (`seatbelt`, empty allowlist); the Agent already defaults the same way, so existing runs are unchanged.
- `on_taint` default stays `warn`; no run is newly *blocked* unless the operator already chose `refuse`.
- Old transcripts (no `sandbox_driver` in `envelope_end`) skip `egress_uncontained`.
- First run after upgrade starts with an empty ledger; taint accrues from then on.

## 9. Open questions (resolve during planning)

- **`grep` granularity:** taint the run when `grep`/`count_matches` scans a glob that *matches* a tainted file? Simpler v1: taint on `read_file` only; treat `grep` as content-returning and taint if any matched file is tainted; leave `count_matches` (counts only) untainted. Decide in planning whether `grep` is in or out for v1. (`BOUNDARY_HOME` test override is resolved — see §3.1.)
