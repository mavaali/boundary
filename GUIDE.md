# Boundary guide

A working manual for Boundary.

If you want the *why* behind the design, see the "Hallucinated Intent and the Envelope Problem" doctrine. This file is the *how*.

---

## What this is

Boundary runs tool-calling agents inside a pre-declared envelope. Three modes:

1. **Interactive** (`boundary run`) — you hand it a persona + task, it runs once
2. **Fielding Coach** (`boundary fielding-coach`) — loose prompt → Fielding Coach proposes an envelope → you approve → execute
3. **Scheduled** (`boundary schedule install ...`) — YAML config → launchd LaunchAgent → runs headless on schedule

Every run is wrapped in an **envelope** (write allowlist, staging pivot, spend caps, ambiguity halt) and can be graded by the **Third Umpire** (property checks against the envelope, not against the agent's "quality").

Lives at `~/projects/boundary/`. Independent of Scout/Clawpilot — works from any shell with Python 3.10+.

---

## One-time setup

```bash
cd ~/projects/boundary
source .venv/bin/activate          # already created tonight
boundary copilot status           # should say "oauth token: present"
```

If status says missing, run:
```bash
boundary copilot login            # device-code flow, opens https://github.com/login/device
```

Token persists to `~/.config/github-copilot/apps.json` — shared with Copilot.vim/Copilot.lua format. Don't lose it.

To see available models:
```bash
boundary copilot models           # lists everything your subscription allows
```

Default model is `claude-sonnet-4.5`. Override with `--model claude-opus-4.7` etc.

---

## Mode 1 — Interactive run (you know what you want)

Use when you have a specific system prompt/persona and a clear task. Skips Fielding Coach.

```bash
boundary run \
  --system "You are a careful researcher. Read narrowly, stage a thesis, then write the allowed output." \
  --workspace ./scratch \
  --envelope-writable "scratch/research-snapshot-$(date +%F).md" \
  --envelope-max-writes 3 \
  --envelope-min-writes 1 \
  --max-iters 25 \
  --task "Your task here..." \
  --verbose
```

Key flags:

| Flag | What it does |
|---|---|
| `--system <text>` / `--system-file <path>` | Provide a generic system prompt |
| `--persona <path>` | Optional adapter: load a charter/prompt file as the system prompt |
| `--workspace <dir>` | All file ops are jailed inside this dir |
| `--clawpilot` | Adds `skill_load`, `charter_load`, `workiq` tools |
| `--web` | Adds `fetch_url` |
| `--envelope-writable <path>` | Activates envelope mode. Repeatable. Paths/globs relative to workspace. |
| `--envelope-min-writes 1` | Forces the agent to actually produce output (budget pressure fires at 60% / 80% of iters if not met) |
| `--envelope-max-writes 3` | Hard cap |
| `--envelope-max-unstaged-reads 3` | Orientation `read_file` calls allowed before `stage_proposal` is required |
| `--no-staging-gate` | Disable the staging pivot for a run (escape hatch; not recommended for analysis tasks) |
| `--envelope-max-iters 25` | Loop budget |
| `--envelope-max-dollars 0.50` | Optional spend cap; halts cleanly |
| `--envelope-max-wall-seconds 600` | Wall-clock safety net (default 900s) |
| `--max-iters 30` | Non-envelope iteration cap |
| `--model claude-opus-4.7` | Override model |

**No persona, just a free-form agent:**
```bash
boundary run --task "..." --workspace ./scratch --system "You are a careful researcher."
```

**With a local overlay skin:**
```bash
boundary run --overlay mihir --role natasha --task "Review this repo"
```

**Output you'll see:**
```
[3] tool_call: write_file({...})
[3] tool_result write_file: wrote 11862 chars to scratch/research-snapshot-2026-06-12.md
...
=== final ===
<agent's summary>
[iterations=17 stop=stop wall=42.3s]
[envelope: writes=1/3 attempted=1 external=0]
[spend: in=78,243 (cached=12,500) out=4,103 est=$0.2569]
[transcript: /Users/mihirwagle/.boundary/transcripts/...]
```

---

## Overlays — local skins without contaminating core

Overlays keep the public harness generic while letting a local setup carry its
own names, workspaces, role packs, and source hierarchy.

```bash
boundary overlays list
boundary overlays show sample
```

An overlay can provide:

| Field | Meaning |
|---|---|
| `default_workspace` | Workspace used when `--workspace` is omitted or `.` |
| `enable_clawpilot` | Whether to enable local Clawpilot bridge tools |
| `roles` | Role name → prompt/charter path mapping |
| `extra_system` | Local guidance appended to loaded prompts |

Example:

```bash
boundary run \
  --overlay sample \
  --role repo-reviewer \
  --envelope-writable "scratch/security-review-$(date +%F).md" \
  --task "Review the shell sandbox."
```

Local overlays can live under `~/.boundary/overlays/<name>/`. Keep overlays
with private paths, source hierarchy, or role names outside the public repo.
Public docs and examples stay generic.

The repo includes `examples/overlays/sample/` as a portable overlay starter.

---

## Mode 2 — Fielding Coach (loose prompt, let the planner figure it out)

Use when you don't yet know which persona/prompt or what scope. Fielding Coach reads optional workspace context, proposes a structured envelope, and asks for approval.

```bash
boundary fielding-coach \
  "review this repo's auth flow and write a concise risk brief to scratch/" \
  --workspace ~/repo
```

You'll see Fielding Coach's proposal:
```markdown
# Fielding Coach proposal
**Restated intent:** ...
**Persona:** researcher
**Writable paths:** ['scratch/auth-risk-brief.md']
**Max writes:** 1 | **Min writes:** 1 | **Max iters:** 20
**Rationale:** ...
**Task (tightened):** ...

[fielding-coach] dispatch this envelope? [y/N/edit]
```

- `y` — execute
- `N` — cancel
- `edit` — not implemented yet; rerun with a tighter prompt instead

To skip the approval gate (e.g., in scripts):
```bash
boundary fielding-coach "..." --workspace ~/repo --auto
```

After dispatch, you get the same envelope/spend output as `run`, plus a Third Umpire report appended automatically.

**When Fielding Coach gets routing wrong:** rerun with a tighter prompt or fall back to Mode 1.

---

## Mode 3 — Scheduled headless (set and forget)

For things you want to run on a cadence: weekly repo review, daily documentation check, etc.

### Write a schedule YAML

Use `examples/schedules/weekly-coverage.yaml` as a template. Save yours anywhere:

```yaml
name: weekly-repo-review
schedule: "weekly mon 09:00"          # see "Schedule syntax" below
persona: researcher
workspace: ~/repo

envelope:
  writable_paths:
    - scratch/weekly-review-{date}.md   # {date} → 2026-06-12
  max_writes: 3
  min_writes: 1
  require_staging: true
  max_unstaged_reads: 3
  max_iters: 25
  max_dollars: 0.50
  max_wall_seconds: 600

on_ambiguity: queue     # queue | fail | best_effort
task: |
  <multi-line task here>
```

### Validate before installing

```bash
boundary schedule validate path/to/your-schedule.yaml
```

Shows what would be installed: parsed schedule, rendered writable paths, caps, persona.

### Test-run without scheduling

```bash
boundary schedule-run path/to/your-schedule.yaml --verbose
```

Runs it now. Records the run in history. Same envelope + Third Umpire enforcement as the real schedule.

### Install for real

```bash
boundary schedule install path/to/your-schedule.yaml
```

This writes a `~/Library/LaunchAgents/io.boundary.schedule.<name>.plist` and bootstraps it into launchd. Survives reboot.

### Manage installed schedules

```bash
boundary schedule list                          # show what's installed
boundary schedule uninstall weekly-coverage     # remove (by schedule name)
```

Logs go to `~/.boundary/launchd-logs/io.boundary.schedule.<name>.{out,err}.log`.

More schedule starters live in `examples/schedules/`:

| File | Use it for |
|---|---|
| `weekly-coverage.yaml` | broad weekly repo review |
| `daily-docs-check.yaml` | short daily documentation drift scan |
| `weekly-risk-review.yaml` | risk-focused code and process review |
| `release-notes-draft.yaml` | convert recent changes into a draft release note |

### Schedule syntax

| String | Meaning |
|---|---|
| `"hourly"` | every 3600s |
| `"every 2h"` / `"every 30m"` | interval |
| `"daily 09:00"` | every day at 09:00 |
| `"weekly mon 09:00"` | every Monday at 09:00 (mon/tue/wed/thu/fri/sat/sun) |
| `"cron:0 9 * * 1"` | raw cron — **not supported on macOS** (launchd doesn't speak cron) |

### Template substitution in YAML

| Token | Resolves to |
|---|---|
| `{date}` | `2026-06-12` |
| `{datetime}` | `2026-06-12T1924` |
| `{name}` | schedule name |

Works in `writable_paths` and inside `task`. **Use `{date}` in writable_paths** to avoid same-schedule runs clobbering each other across days.

### `on_ambiguity` policy

| Value | Behavior |
|---|---|
| `queue` (default) | Agent's `ask_human()` halts loop. Question + transcript queued to DB. You handle via `review-queue`. |
| `fail` | Exit non-zero. launchd captures in stderr log. |
| `best_effort` | Injects "no human available, label assumptions [HYPOTHESIS], proceed" into system prompt. Agent never calls `ask_human`. |

Use `queue` for anything you care about. Use `best_effort` for fully automated content (a daily summary email that should always send). Use `fail` if you want launchd to surface the failure loudly.

### Run-lock

The headless runner takes a PID-based lock per schedule name. If a prior run is still active when the next trigger fires, the new trigger logs `[skip] previous run still active` instead of clobbering. Stale locks (process dead) get stolen automatically.

---

## Reading the outputs

### Transcripts

Every run writes a JSONL transcript to `~/.boundary/transcripts/<ts>-<persona>-<pid>.jsonl`.

Each line is one event: `request`, `assistant`, `tool_result`, `envelope_start`, `budget_pressure`, `envelope_end`, etc.

Useful one-liners:

```bash
# pretty-print last transcript
ls -t ~/.boundary/transcripts/*.jsonl | head -1 | xargs -I{} jq . {} | less

# extract just the assistant turns
jq -r 'select(.type=="assistant") | "[" + (.iteration|tostring) + "] " + (.content // "")' <transcript>

# which charter version produced this?
jq -r 'select(.type=="charter_version")' <transcript>

# how much did this run cost?
jq -r 'select(.type=="envelope_end") | {tokens_in: .input_tokens, tokens_out: .output_tokens, est_dollars: .estimated_dollars}' <transcript>
```

### Third Umpire reports

Grade any transcript at any time:

```bash
boundary third-umpire ~/.boundary/transcripts/<file>.jsonl
```

Verdict is `PASS` / `WARN` / `FAIL`. 11 checks, severity-graded. `FAIL` exit code is 2.

For envelope runs with staging enabled, Third Umpire also reports
`staging_pivot`: whether `stage_proposal` happened before the first write and
whether the run hit any staging refusals. This is the anti-boil-the-ocean check.

### Run history

```bash
boundary history                          # last 20 runs
boundary history --limit 50
boundary history --schedule weekly-competitive-coverage
```

Output columns: id, timestamp, schedule name, persona, stop reason, Third Umpire verdict, writes, $, wall.

### Review queue

```bash
boundary review-queue                                  # list open ambiguity halts
boundary review-queue resolve 7 "yes, target Q3 instead"   # mark resolved with note
```

When an agent calls `ask_human` and you weren't there, the question + options + transcript path land here. Process them on your own time. Resolving doesn't rerun the schedule — you decide whether to manually `schedule-run` again.

---

## Editing personas / prompt files

Generic runs can use `--system` or `--system-file`. If your workspace has a role pack under `.squad/agents/<name>/charter.md`, `--persona` can load those prompt files directly.

Things to know:

- **Charter SHA is logged per run.** If you edit a charter, transcripts before/after have different `charter_sha` values. Grep to bucket.
- **Charter changes don't invalidate provider caches** but cost math is conservative regardless.
- **The harness appends an envelope note** to the charter at runtime. You don't need to write envelope-aware instructions into the charter itself.
- **Personas have `ask_human` available** in envelope mode. Charters should encourage using it when blocked rather than guessing.

To add a new role-pack persona:
1. Drop `charter.md` in `.squad/agents/<name>/`
2. Reference it: `--persona ~/.../.squad/agents/<name>/charter.md`
3. Fielding Coach picks it up automatically next time the personas list is read

---

## Staging pivot — anti-boil-the-ocean analysis

Envelope mode now adds one analysis primitive: `stage_proposal`.

This is not a second budget. Reads still do **not** debit the write budget. The rule is simpler: after a small orientation window, the agent must stage a provisional answer before doing more deep reads or any write.

### Why

The failure mode this prevents is:

```
read everything → synthesize late → write at the end
```

The desired loop is:

```
candidate answer → smallest discriminating read → revise or commit
```

The staging pivot asks the same opportunity-value question at the read/write boundary:

> Is the marginal value of this next action greater than the value of acting now?

For reads, the action is "one more file." For writes, the action is "spend irreversibility budget on this output." The staging area is the pivot between those two questions.

### Runtime behavior

When an envelope has writable paths:

1. The agent may make up to `max_unstaged_reads` orientation `read_file` calls.
2. More `read_file` calls are refused until the agent calls `stage_proposal`.
3. Writes and commit tools are refused until the agent calls `stage_proposal`.
4. After staging, further reads are allowed, but must test, kill, or strengthen the staged answer.
5. If a write is refused, the agent must revise from the same staged proposal and reads — no restarting the read phase.

`stage_proposal` requires:

| Field | Meaning |
|---|---|
| `thesis` | Answer-first tentative conclusion |
| `hypotheses` | 2-3 lines that could explain or falsify the answer |
| `evidence_plan` | Smallest reads needed to discriminate between hypotheses |
| `intended_write` | Output path/action if known |
| `cost_class` | Rough action class, e.g. review, patch, email, commit |
| `kill_criteria` | What would stop this line of inquiry |

The point is Minto + Book of Why discipline: hypothesis-driven analysis, not source collection.

---

## Commit tools — irreversible external actions

A **commit tool** is anything that changes state outside the workspace and can't be unwound: send email, post Teams message, push commit, file ADO bug, submit expense, charge a card. They're treated as a distinct tool kind (`kind="commit"`) and gated separately from `read`/`write`/`external`.

The envelope's bet: bound the *output* of cognitive work via writable_paths + max_writes; bound the *commitments* via the commit policy. The two layers are independent.

### Policies

| Policy | Behavior |
|---|---|
| `refuse` | Any commit attempt is refused. **Default for headless runs.** Safe choice. |
| `queue` | Halts the run and queues a review entry (mirrors `ambiguity_halt`). Resume via `boundary review-queue`. |
| `ask` | Interactive only: refuses and instructs the agent to call `ask_human`. Then the human decides. |
| `allow` | Executes commit tools. Requires `commit_allowlist` enumerating which tools — empty list under `allow` means ALL commit tools (foot-gun, validated against). |

### Mode-by-mode

**`boundary run --envelope-writable ...`** (interactive)
If commit tools are registered and `--on-commit` is not passed, you'll be prompted: `r/q/a` (default `r`). `allow` requires explicit flags:

```bash
boundary run ... --on-commit allow --commit-allow bash_commit
```

**`boundary fielding-coach "prompt"`** (interactive planner)
Same prompt — Fielding Coach dispatch enables shell by default, so `bash_commit` is in the registry.

**`boundary schedule install <yaml>`** (headless)
YAML must declare `on_commit` if it's anything other than the default `refuse`. Install fails on bad combinations:

```yaml
name: weekly-digest
schedule: weekly mon 09:00
persona: researcher
workspace: ~/repo
task: "..."
envelope:
  writable_paths: ["scratch/digest-{date}.md"]
on_commit: allow
commit_allowlist:
  - bash_commit   # for `gh issue update`, etc.
```

Validation errors block install:
- `on_commit: allow` with empty `commit_allowlist` (probably a mistake)
- `commit_allowlist` set but `on_commit != allow` (would be ignored)
- `on_commit` not in {refuse, queue, allow}

### The bash loophole: the egress proxy is the boundary, the denylist is a nudge

Bash can sidestep typed commit tools for external side effects: `curl -X POST`, `gh issue create`, `python -c "urllib..."`. There are two layers here with **deliberately different jobs** — and only one of them is a security boundary.

**1. OS-enforced egress (the actual boundary).** With `--sandbox-driver srt`, `bash` and every process it spawns run under [srt](https://github.com/anthropic-experimental/sandbox-runtime), which enforces a network egress allowlist over the *entire process tree* (plus a cross-platform workspace write-jail). Egress to anything not on `--egress-allow` is blocked at the OS/proxy level — **regardless of how the request is spawned**: `curl`, a copied/renamed binary, `python -c urllib`, `nc`, all blocked. An empty allowlist means no network. This is the enforcement boundary.

> The default driver is `seatbelt` (macOS write-jail only, egress **not** bounded). Opt into `--sandbox-driver srt` for egress enforcement. See "Security boundary" below.

**2. The basename denylist (an intent nudge, not containment).** On the regular `bash` tool, commands whose first token (basename, after an optional env-var prefix) is one of:

```
curl, wget, gh, az, mail, sendmail, osascript, git (push|commit|tag only)
```

are refused with "use `bash_commit` instead." This is **not** a containment mechanism — it is bypassable by construction (`./renamed-curl`, a copied binary, `python -c urllib`). Its only job is to make the *common* commit paths require explicit `bash_commit` intent, nudging an agent reaching for `gh issue create` onto the typed, policy-gated path. Layer 1 is what actually stops exfiltration.

**Slope guardrails — the denylist is frozen:**
- **Hard cap of 12 entries, and no new entries.** It's an intent nudge; lengthening it chases a containment property it structurally cannot have. Bypasses are stopped by the egress proxy, not this list.
- **No regex. No argument inspection** except the single `git` subcommand exception.
- **For a real boundary on a new binary, use `--sandbox-driver srt` with an egress allowlist** — or build a typed `kind="commit"` tool. Don't grow this list into a policy DSL.
- **Third Umpire surfaces every `bash_commit` and commit-tool call in its verdict.** If an agent shells out to `gh` repeatedly, the answer is `gh_create_issue` as a typed commit tool.

### Third Umpire output

Third Umpire reports commit activity in the summary block:
- `commit_attempted` / `commit_executed` counts
- `on_commit` policy in effect
- `commit_allowlist`

And adds two checks:
- **commit_policy_held** — FAIL if a commit executed under `refuse`/`queue`/`ask`, or under `allow` but not in the allowlist.
- **bash_egress_denylist** — WARN (informational) for every blocked bash command, with a reminder to build a typed tool instead of extending the denylist.

---

## Taint / provenance — the write-as-exfil channel

The envelope bounds *which* path, *how many* writes, and *which* commits. It does
not, by default, bound *what content* flows into an allowed write. An agent can
read an untrusted file or web page carrying an injection and write exfiltrated
content into a perfectly-allowlisted path that later syncs or shares — the
writable path becomes an exfiltration channel. That is the lethal trifecta's
third leg.

The **taint gate** (`--on-taint`, or `on_taint:` in a schedule YAML) closes it
with coarse, run-level tracking:

- Reading untrusted external content (`fetch_url`) marks the run **tainted**.
- After that, any write/commit to a writable sink trips a `taint_flow` event.
- `--on-taint` decides what happens:
  - `warn` (default) — record the `taint_flow` event, let the write proceed.
  - `refuse` — block the write; untrusted content must not reach a writable sink.
  - `allow` — disable the check (a **downgrade**; the Third Umpire flags it).

A run that reads only workspace files and writes does **not** trip the gate — no
false positive on the common case. The Third Umpire surfaces a `taint_flow` check
in its verdict, and `stage_proposal` records the taint set that fed the thesis.

> **Coarse by design.** Once *any* untrusted source is read, all later writes are
> flagged — it does not track which bytes flowed where. `warn` is the safe
> default (a verdict line, not a block). `refuse` is aggressive: it blocks *every*
> write post-taint, so reserve it for runs where no untrusted→write flow is ever
> legitimate. Per-value / per-sink granularity is future work.

---

## Cost / budget knobs

### Defaults per run

| Knob | Default | Override |
|---|---|---|
| `max_input_tokens` | 500,000 | `--envelope-max-input-tokens N` |
| `max_output_tokens` | 50,000 | `--envelope-max-output-tokens N` |
| `max_dollars` | None (off) | `--envelope-max-dollars 0.50` |
| `max_wall_seconds` | 900 (15 min) | `--envelope-max-wall-seconds N` |
| `max_iters` | 25 | `--max-iters N` |
| `max_writes` | 10 | `--envelope-max-writes N` |
| `min_writes` | 1 | `--envelope-min-writes N` |
| `max_external` | 20 | `--envelope-max-external N` |

### What things actually cost (Sonnet 4.5)

A real research run: ~80K input + 4K output ≈ **$0.30**.
Same task on Opus 4.7: ~$1.50.
Same task on Haiku 4.5: ~$0.08.

Cached input is ~10× cheaper. On repeated similar tasks over the same workspace, expect 50-80% cache hit rate after the first run.

### Setting a hard $ ceiling

```bash
boundary run ... --envelope-max-dollars 0.25
```

Third Umpire reports `budget_halt` as WARN if the run was cut off, plus exact spend.

---

## Security boundary

For where Boundary sits in the field — what the envelope defends and doesn't,
mapped onto the lethal trifecta and the six secure-agent design patterns, plus
how it compares to neighbors (predicate-secure, Cupcake, nah) — see
[Where Boundary sits](README.md#where-boundary-sits) in the README.

Boundary has three practical safety layers:

1. The workspace boundary controls where file tools operate.
2. The envelope controls which paths the agent may write, append, or commit.
3. The **sandbox driver** controls the OS containment for `bash` (`--sandbox-driver`).

**Sandbox drivers** (`--sandbox-driver`, default `seatbelt`):

- `seatbelt` — macOS `sandbox-exec`; local writes restricted to the workspace + its
  temp dir. A **write boundary only** — network egress is *not* bounded.
- `srt` — [Anthropic sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime):
  Seatbelt (macOS) / bubblewrap (Linux) / WFP (Windows) **plus a network egress
  allowlist** enforced over the whole process tree. Set allowed domains with
  `--egress-allow` (empty = no network). Requires `npm i -g @anthropic-ai/sandbox-runtime`.
- `none` — no OS sandbox (loud, explicit opt-out).

Even under `seatbelt`, the following are NOT bounded — use `srt`, a dedicated OS
user, or a container for sensitive work:

- shell commands may still read files the current OS user can read;
- network egress is not bounded (use `--sandbox-driver srt --egress-allow ...`);
- tools enabled through local adapters may expose additional capabilities;
- commit-class tools are governed by the commit policy, but read-only tools can
  still surface sensitive data into the transcript.

For sensitive work, prefer `--sandbox-driver srt` with a tight `--egress-allow`,
disable shell with `--no-shell`, leave web disabled unless needed, and keep
private overlays under `~/.boundary/overlays/` rather than in the repo.

Copilot OAuth tokens are stored in `~/.config/github-copilot/apps.json`. Agent
Kit refuses to load that file if group or world permissions are set; fix with:

```bash
chmod 600 ~/.config/github-copilot/apps.json
```

---

## Troubleshooting

### "ENVELOPE REFUSED: path 'X' is not in writable_paths"

You forgot to allowlist the path. Re-run with `--envelope-writable "X"` or update the YAML.

### Run hung / over wall-clock

Likely a stalled provider call. Wall-clock cap kicks in. Check transcript for the last tool call; if it's `fetch_url` or `bash`, the remote/process stalled. Wall-clock cap defaults to 900s — lower for chatty tasks.

### "skipped_locked" in history

Previous run with same schedule name still in progress. Wait for it, or remove `~/.boundary/locks/<name>.lock` manually if you're sure it's stale (the PID-alive check should handle this — if you're hitting it, file a bug).

### Fielding Coach routes to the wrong persona

Two fixes:
1. Mention the persona/prompt in your request.
2. Update optional workspace routing docs so future Fielding Coach calls see better routing rules.

### Third Umpire says `FAIL: produced_output`

Agent didn't write. Either bumped into ambiguity (check transcript for `ask_human`), the task was too narrow to need output, or `max_iters` was too small for the read-budget the task needed. Raise `min_writes` and the budget-pressure system will nudge harder.

### Token usage shows 0

Old transcript pre-instrumentation, or the provider didn't return `usage`. Together is the most likely culprit — recent versions are fine. If it persists, check `clients/together.py`.

### "device-code login failed: expired_token"

The 15-min code window passed. Rerun `boundary copilot login` and approve faster.

### "Copilot OAuth token file is too permissive"

Boundary refuses to read token files that are group- or world-readable. Run:

```bash
chmod 600 ~/.config/github-copilot/apps.json
```

---

## File locations

| What | Where |
|---|---|
| Source | `~/projects/boundary/` |
| Venv | `~/projects/boundary/.venv/` |
| Copilot token | `~/.config/github-copilot/apps.json` |
| Transcripts | `~/.boundary/transcripts/*.jsonl` |
| Run history DB | `~/.boundary/history.db` |
| Per-schedule locks | `~/.boundary/locks/<name>.lock` |
| launchd plists | `~/Library/LaunchAgents/io.boundary.schedule.*.plist` |
| launchd logs | `~/.boundary/launchd-logs/*.log` |
| Example schedules | `~/projects/boundary/examples/schedules/` |

---

## When to use which mode

- **You're poking at something exploratory** → Mode 2 (`fielding-coach`). Lets you stay loose; Fielding Coach forces precision.
- **You know exactly what you want, one-off** → Mode 1 (`run`). Skip Fielding Coach.
- **Recurring task you'd otherwise forget** → Mode 3 (`schedule install`).
- **Reviewing whether things are working at all** → `boundary history` once a week.

---

## What's not built yet

If you find yourself wanting these, here's the queue:

1. **`charter_scope_match`** Third Umpire check — validate Fielding Coach routed within persona's "What I Own"
2. **Daily digest** via `workiq_send_email` / Teams DM (instead of just CLI `history`)
3. **Provenance tags** on written files for cross-run staleness detection
4. **`schedule install` conflict warning** when writable paths overlap with existing schedule
5. **Multi-agent chains** (role A → role B → role C as one envelope)

None of these are urgent. Add when you actually hit the failure they solve.
