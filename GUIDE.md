# agent-kit guide

A working manual for `~/projects/agent-kit/`. Read this when you come back to it after a week off.

If you want the *why* behind the design, see `~/projects/agent-kit/DESIGN.md` and your "Hallucinated Intent and the Envelope Problem" blog post. This file is the *how*.

---

## What this is

A standalone Python harness that runs your Avengers/West Wing/Dream XI personas as real tool-calling agents. Three modes:

1. **Interactive** (`agent-kit run`) — you hand it a persona + task, it runs once
2. **Captain** (`agent-kit stark`) — loose prompt → Stark proposes an envelope → you approve → execute
3. **Scheduled** (`agent-kit schedule install ...`) — YAML config → launchd LaunchAgent → runs headless on schedule

Every run is wrapped in an **envelope** (write allowlist, spend caps, ambiguity halt) and auto-graded by **Fury** (11 property checks against the envelope, not against the agent's "quality").

Lives at `~/projects/agent-kit/`. Independent of Scout/Clawpilot — works from any shell with Python 3.10+.

---

## One-time setup

```bash
cd ~/projects/agent-kit
source .venv/bin/activate          # already created tonight
agent-kit copilot status           # should say "oauth token: present"
```

If status says missing, run:
```bash
agent-kit copilot login            # device-code flow, opens https://github.com/login/device
```

Token persists to `~/.config/github-copilot/apps.json` — shared with Copilot.vim/Copilot.lua format. Don't lose it.

To see available models:
```bash
agent-kit copilot models           # lists everything your subscription allows
```

Default model is `claude-sonnet-4.5`. Override with `--model claude-opus-4.7` etc.

---

## Mode 1 — Interactive run (you know what you want)

Use when you have a specific persona and a clear task. Skips Stark.

```bash
agent-kit run \
  --persona ~/projects/FabricSpecs/.squad/agents/banner/charter.md \
  --workspace ~/projects/FabricSpecs \
  --clawpilot \
  --envelope-writable "scratch/banner-snapshot-$(date +%F).md" \
  --envelope-max-writes 3 \
  --envelope-min-writes 1 \
  --max-iters 25 \
  --task "Your task here..." \
  --verbose
```

Key flags:

| Flag | What it does |
|---|---|
| `--persona <path>` | Loads `.squad/agents/<name>/charter.md` as system prompt |
| `--workspace <dir>` | All file ops are jailed inside this dir |
| `--clawpilot` | Adds `skill_load`, `charter_load`, `workiq` tools |
| `--web` | Adds `fetch_url` |
| `--envelope-writable <path>` | Activates envelope mode. Repeatable. Paths/globs relative to workspace. |
| `--envelope-min-writes 1` | Forces the agent to actually produce output (budget pressure fires at 60% / 80% of iters if not met) |
| `--envelope-max-writes 3` | Hard cap |
| `--envelope-max-iters 25` | Loop budget |
| `--envelope-max-dollars 0.50` | Optional spend cap; halts cleanly |
| `--envelope-max-wall-seconds 600` | Wall-clock safety net (default 900s) |
| `--max-iters 30` | Non-envelope iteration cap |
| `--model claude-opus-4.7` | Override model |

**No persona, just a free-form agent:**
```bash
agent-kit run --task "..." --workspace ./scratch --system "You are a careful researcher."
```

**Output you'll see:**
```
[3] tool_call: write_file({...})
[3] tool_result write_file: wrote 11862 chars to scratch/banner-snapshot-2026-06-12.md
...
=== final ===
<agent's summary>
[iterations=17 stop=stop wall=42.3s]
[envelope: writes=1/3 attempted=1 external=0]
[spend: in=78,243 (cached=12,500) out=4,103 est=$0.2569]
[transcript: /Users/mihirwagle/.agent-kit/transcripts/...]
```

---

## Mode 2 — Captain (loose prompt, let Stark figure it out)

Use when you don't yet know which persona or what scope. Stark reads `.squad/routing.md` + persona list, proposes structured envelope, you approve.

```bash
agent-kit stark \
  "do a quick competitive coverage check on Snowflake & Databricks, put it in scratch/" \
  --workspace ~/projects/FabricSpecs
```

You'll see Stark's proposal:
```markdown
# Stark proposal
**Restated intent:** ...
**Persona:** banner
**Writable paths:** ['scratch/competitive-snapshot.md']
**Max writes:** 1 | **Min writes:** 1 | **Max iters:** 20
**Rationale:** ...
**Task (tightened):** ...

[stark] dispatch this envelope? [y/N/edit]
```

- `y` — execute
- `N` — cancel
- `edit` — not implemented yet; rerun with a tighter prompt instead

To skip the approval gate (e.g., in scripts):
```bash
agent-kit stark "..." --workspace ~/repo --auto
```

After dispatch, you get the same envelope/spend output as `run`, plus a Fury report appended automatically.

**When Stark gets routing wrong:** rerun with persona name hinted in the prompt ("…have Vision draft this…") or fall back to Mode 1.

---

## Mode 3 — Scheduled headless (set and forget)

For things you want to run on a cadence: weekly competitive check, daily wiki maintenance, etc.

### Write a schedule YAML

Use `examples/schedules/weekly-coverage.yaml` as a template. Save yours anywhere:

```yaml
name: weekly-competitive-coverage
schedule: "weekly mon 09:00"          # see "Schedule syntax" below
persona: banner
workspace: ~/projects/FabricSpecs

envelope:
  writable_paths:
    - scratch/weekly-coverage-{date}.md   # {date} → 2026-06-12
  max_writes: 3
  min_writes: 1
  max_iters: 25
  max_dollars: 0.50
  max_wall_seconds: 600

on_ambiguity: queue     # queue | fail | best_effort
task: |
  <multi-line task here>
```

### Validate before installing

```bash
agent-kit schedule validate path/to/your-schedule.yaml
```

Shows what would be installed: parsed schedule, rendered writable paths, caps, persona.

### Test-run without scheduling

```bash
agent-kit schedule-run path/to/your-schedule.yaml --verbose
```

Runs it now. Records the run in history. Same envelope + Fury enforcement as the real schedule.

### Install for real

```bash
agent-kit schedule install path/to/your-schedule.yaml
```

This writes a `~/Library/LaunchAgents/io.agent-kit.schedule.<name>.plist` and bootstraps it into launchd. Survives reboot.

### Manage installed schedules

```bash
agent-kit schedule list                          # show what's installed
agent-kit schedule uninstall weekly-coverage     # remove (by schedule name)
```

Logs go to `~/.agent-kit/launchd-logs/io.agent-kit.schedule.<name>.{out,err}.log`.

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

The headless runner takes a PID-based lock per schedule name. If a Monday Banner run is still going when next Monday hits, the new trigger logs `[skip] previous run still active` instead of clobbering. Stale locks (process dead) get stolen automatically.

---

## Reading the outputs

### Transcripts

Every run writes a JSONL transcript to `~/.agent-kit/transcripts/<ts>-<persona>-<pid>.jsonl`.

Each line is one event: `request`, `assistant`, `tool_result`, `envelope_start`, `budget_pressure`, `envelope_end`, etc.

Useful one-liners:

```bash
# pretty-print last transcript
ls -t ~/.agent-kit/transcripts/*.jsonl | head -1 | xargs -I{} jq . {} | less

# extract just the assistant turns
jq -r 'select(.type=="assistant") | "[" + (.iteration|tostring) + "] " + (.content // "")' <transcript>

# which charter version produced this?
jq -r 'select(.type=="charter_version")' <transcript>

# how much did this run cost?
jq -r 'select(.type=="envelope_end") | {tokens_in: .input_tokens, tokens_out: .output_tokens, est_dollars: .estimated_dollars}' <transcript>
```

### Fury reports

Grade any transcript at any time:

```bash
agent-kit fury ~/.agent-kit/transcripts/<file>.jsonl
```

Verdict is `PASS` / `WARN` / `FAIL`. 11 checks, severity-graded. `FAIL` exit code is 2.

### Run history

```bash
agent-kit history                          # last 20 runs
agent-kit history --limit 50
agent-kit history --schedule weekly-competitive-coverage
```

Output columns: id, timestamp, schedule name, persona, stop reason, Fury verdict, writes, $, wall.

### Review queue

```bash
agent-kit review-queue                                  # list open ambiguity halts
agent-kit review-queue resolve 7 "yes, target Q3 instead"   # mark resolved with note
```

When an agent calls `ask_human` and you weren't there, the question + options + transcript path land here. Process them on your own time. Resolving doesn't rerun the schedule — you decide whether to manually `schedule-run` again.

---

## Editing personas

You're not editing this repo — you're editing `~/projects/FabricSpecs/.squad/agents/<name>/charter.md`. Same files you've always had.

Things to know:

- **Charter SHA is logged per run.** If you edit a charter, transcripts before/after have different `charter_sha` values. Grep to bucket.
- **Charter changes don't invalidate provider caches** but cost math is conservative regardless.
- **The harness appends an envelope note** to the charter at runtime. You don't need to write envelope-aware instructions into the charter itself.
- **Personas have `ask_human` available** in envelope mode. Charters should encourage using it when blocked rather than guessing.

To add a new persona:
1. Drop `charter.md` in `.squad/agents/<name>/`
2. Reference it: `--persona ~/.../.squad/agents/<name>/charter.md`
3. Stark picks it up automatically next time he reads the personas list

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

A real Banner run from tonight: ~80K input + 4K output ≈ **$0.30**.
Same task on Opus 4.7: ~$1.50.
Same task on Haiku 4.5: ~$0.08.

Cached input is ~10× cheaper. On repeated similar tasks (e.g., daily Banner check on the same wiki), expect 50-80% cache hit rate after the first run.

### Setting a hard $ ceiling

```bash
agent-kit run ... --envelope-max-dollars 0.25
```

Fury reports `budget_halt` as WARN if the run was cut off, plus exact spend.

---

## Troubleshooting

### "ENVELOPE REFUSED: path 'X' is not in writable_paths"

You forgot to allowlist the path. Re-run with `--envelope-writable "X"` or update the YAML.

### Run hung / over wall-clock

Likely a stalled provider call. Wall-clock cap kicks in. Check transcript for the last tool call; if it's `fetch_url` or `bash`, the remote/process stalled. Wall-clock cap defaults to 900s — lower for chatty tasks.

### "skipped_locked" in history

Previous run with same schedule name still in progress. Wait for it, or remove `~/.agent-kit/locks/<name>.lock` manually if you're sure it's stale (the PID-alive check should handle this — if you're hitting it, file a bug).

### Stark routes to the wrong persona

Two fixes:
1. Mention the persona in your prompt: "have Banner do …"
2. Update `.squad/routing.md` so future Stark calls see better routing rules

### Fury says `FAIL: produced_output`

Agent didn't write. Either bumped into ambiguity (check transcript for `ask_human`), the task was too narrow to need output, or `max_iters` was too small for the read-budget the task needed. Raise `min_writes` and the budget-pressure system will nudge harder.

### Token usage shows 0

Old transcript pre-instrumentation, or the provider didn't return `usage`. Together is the most likely culprit — recent versions are fine. If it persists, check `clients/together.py`.

### "device-code login failed: expired_token"

The 15-min code window passed. Rerun `agent-kit copilot login` and approve faster.

---

## File locations

| What | Where |
|---|---|
| Source | `~/projects/agent-kit/` |
| Venv | `~/projects/agent-kit/.venv/` |
| Copilot token | `~/.config/github-copilot/apps.json` |
| Transcripts | `~/.agent-kit/transcripts/*.jsonl` |
| Run history DB | `~/.agent-kit/history.db` |
| Per-schedule locks | `~/.agent-kit/locks/<name>.lock` |
| launchd plists | `~/Library/LaunchAgents/io.agent-kit.schedule.*.plist` |
| launchd logs | `~/.agent-kit/launchd-logs/*.log` |
| Example schedules | `~/projects/agent-kit/examples/schedules/` |

---

## When to use which mode

- **You're poking at something exploratory** → Mode 2 (`stark`). Lets you stay loose, Stark forces precision.
- **You know exactly what you want, one-off** → Mode 1 (`run`). Skip Stark.
- **Recurring task you'd otherwise forget** → Mode 3 (`schedule install`).
- **Reviewing whether things are working at all** → `agent-kit history` once a week.

---

## What's not built yet

If you find yourself wanting these, here's the queue:

1. **`charter_scope_match`** Fury check — validate Stark routed within persona's "What I Own"
2. **Daily digest** via `workiq_send_email` / Teams DM (instead of just CLI `history`)
3. **Provenance tags** on written files for cross-run staleness detection
4. **`schedule install` conflict warning** when writable paths overlap with existing schedule
5. **Multi-agent chains** (Banner → Vision → Pepper as one envelope)

None of these are urgent. Add when you actually hit the failure they solve.
