# Boundary

[![selftest](https://github.com/mavaali/boundary/actions/workflows/selftest.yml/badge.svg)](https://github.com/mavaali/boundary/actions/workflows/selftest.yml)

**Agents do not need more trust. They need a boundary.**

Boundary runs tool-calling agents inside an explicit envelope: what they may
read, what they may write, when they must stage a thesis, and how the run gets
reviewed afterward. It is for the moment when a coding agent is useful enough to
delegate to, but not safe enough to leave unsupervised.

**Four modes:**
- Interactive: `boundary run --system-file <prompt.md> --task "..."`
- Fielding Coach: `boundary fielding-coach "loose prompt" --workspace <dir>`
- Scheduled: `boundary schedule install <yaml>` (macOS launchd / Windows Task Scheduler)
- Pipelines: `boundary pipeline-run <yaml>` for squad-planned multi-persona jobs

Every envelope run can be reviewed by the **Third Umpire**: property checks against the envelope spec, not against the agent's prose quality.

## Overlays

Keep the core generic and put local skins in overlays:

```bash
boundary overlays list
boundary overlays show sample
boundary run --overlay sample --role repo-reviewer --task "Review this repo"
```

An overlay can provide role names, default workspace, optional bridge tools, and
extra system guidance without changing the generic engine.

## Security boundary

Boundary enforces a workspace write boundary and envelope write allowlist via a
pluggable sandbox driver (`--sandbox-driver`):

- `auto` (default) — prefer the strongest sandbox available: `srt` if installed
  (OS-enforced egress), otherwise fall back to `seatbelt` on macOS **with a loud
  warning that egress is uncontained**, and refuse outright where neither is
  available rather than silently dropping the jail. The secure path is the
  default, not opt-in.
- `seatbelt` — macOS write-jail; blocks local writes outside the
  workspace, but **does not bound network egress** and reads are unrestricted.
- `srt` — [Anthropic sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime)
  (Seatbelt/bubblewrap/WFP) adds an **OS-enforced network egress allowlist** over
  the whole process tree. Set `--egress-allow <domain>` (empty = no network);
  needs `npm i -g @anthropic-ai/sandbox-runtime`. Requesting `srt` explicitly is
  strict — it fails loudly if srt is absent rather than degrading.
- `none` — no sandbox.

Two opt-in controls harden the posture further (both enforced only under `srt`
for reads / require `srt` for bash respectively):

- `--deny-read <path>` (repeatable) hides paths from the jailed bash process, and
  `--deny-read-secrets` also hides a built-in set of common credential locations
  (`~/.aws`, `~/.ssh`, `~/.config/gh`, …). In a schedule YAML: `deny_read: [...]`
  and `deny_read_secrets: true`. Reads are unrestricted on `seatbelt`/`none`, so
  the denylist is enforced only under `srt` (other drivers warn).
- `--require-srt-for-bash` (envelope `require_srt_for_bash: true`) refuses the
  `bash` tool unless the driver is `srt`. `seatbelt`/`none` do not bound egress,
  and the commit-class bash denylist is a nudge, not a boundary — this makes a
  run fail closed rather than shell out with uncontained egress.

For sensitive work, prefer `--sandbox-driver srt` with a tight egress allowlist
(or run as a dedicated OS user / inside a container), add `--deny-read-secrets`
and `--require-srt-for-bash`, and disable shell or web tools when not needed.

## Spend control

An agent you leave running is an agent spending money. Boundary treats spend as a
first-class boundary, not a footnote — the same "bound it, then verify it" posture
it applies to writes and egress. Six composable primitives: five that take a run
from one-shot to fleet-wide, plus attribution to slice the bill.

```
per-run caps ─▶ fail-closed pricing ─▶ spend gradient ─▶ degrade-to-cheaper ─▶ cross-run budgets
   one run          honest cost          soft landing        cheaper tail        many runs
                               + cost attribution: slice any of it by project / tenant
```

### 1. Per-run caps

Every run is bounded by input/output tokens, an optional dollar ceiling, and a
wall-clock kill switch. On the CLI:

```bash
boundary run ... \
  --envelope-max-input-tokens 500000 \
  --envelope-max-output-tokens 50000 \
  --envelope-max-dollars 0.25 \
  --envelope-max-wall-seconds 900
```

or in a schedule/pipeline YAML:

```yaml
envelope:
  max_input_tokens: 500000
  max_output_tokens: 50000
  max_dollars: 0.25
```

### 2. Fail-closed pricing

The dollar cap can only bind for a model the rate card knows. An **unlisted**
model used to estimate at `$0.00` and sail past `max_dollars` entirely — an
unpriced model was an *uncapped* run. Unknown models are now priced at a
conservative upper bound so the cap still bites; the live banner shows
`rate=fallback`.

```yaml
envelope:
  on_unpriced_model: max_rate   # default; "zero" restores fail-open, "<model-id>" borrows a rate
```

The same honesty applies to caching: cache *reads* are cheap (~0.1× input) but
cache *writes* carry a ~1.25× premium, and each is priced on its own axis —
billing a write as fresh input undercounts cached workloads.

### 3. Spend gradient — a soft landing, not a wall

Hitting a cap mid-write wastes the tokens already spent on an unfinished
artifact. Before the hard halt at 100%, the agent is nudged to converge at
fractions of whichever cap is closest to breach:

```yaml
envelope:
  spend_pressure_at: [0.75, 0.9]   # nudge at 75% and 90%; [] disables
```

At 90% the agent sees *"spend at 90% of cap … converge now: finish your current
write and stop"* and a `spend_pressure` event is logged.

### 4. Degrade-to-cheaper-model

Instead of only nudging, swap the run onto a cheaper model once spend crosses a
threshold — the expensive model does the early, high-leverage reasoning; the
cheap one finishes under budget pressure. Spend is accounted per response, so
the post-swap tail is priced at the cheaper rate.

```yaml
envelope:
  max_dollars: 1.00
  degrade_to: claude-haiku-4.5   # a model in the rate card
  degrade_at: 0.6                # swap at 60% of the cap
```

### 5. Cross-run budgets

Per-run caps bound *one* run. A schedule firing hourly at `$0.50`/run has no
ceiling on the **sum**. A `budget:` block bounds spend across runs over calendar
windows (daily/weekly/monthly, calendar-reset) and a trailing rolling window,
aggregated over the run-history ledger — the same data `boundary history` reads,
so there's no second store to drift:

```yaml
budget:
  daily: 5.00
  weekly: 20.00
  monthly: 60.00
  rolling: 3.00
  rolling_hours: 6
  scope: workspace     # or "global"
```

At run time a run whose window is already spent out is **skipped** before any
cost (`stop_reason: skipped_budget`); otherwise its per-run `max_dollars` is
**clamped** to the tightest remaining headroom, so primitives 3–4 enforce the
cross-run ceiling from inside the run.

### 6. Cost attribution

Stamp arbitrary tags on every run so the ledger can be sliced — and budgets
scoped — per project, purpose, or tenant:

```yaml
attribution:
  tenant: acme
  project: pricing
budget:
  monthly: 50.00
  scope: "tag:tenant"   # one $50/mo budget PER tenant, summed across workspaces
```

Interactive runs can be tagged too — envelope-mode `boundary run` records an
`(adhoc)` ledger row carrying `--attribution key=value` tags, so ad-hoc spend is
sliceable alongside scheduled runs:

```bash
boundary run ... --envelope-writable scratch/note.md \
  --attribution tenant=acme --attribution project=pricing --task "..."
```

Inspect any config's live status (exit code `3` when a window is exhausted, so
cron/CI can branch on it):

```bash
boundary budget path/to/schedule.yaml
#  budget  scope=tag:tenant=acme  workspace=/work/acme
#    monthly  $3.5000 / $50.00   $46.5000 left
#    -> ok; binding=monthly; next run capped at $0.2800
```

Full reference and run-cost ballparks: **[GUIDE.md](GUIDE.md)** → *Cost / budget knobs*.

## Where Boundary sits

Boundary's category is **authorization + post-run verification** for tool-calling
agents. Here is what the envelope defends and what it doesn't — stated plainly,
because differentiation by silence reads as ignorance of the field.

### The lethal trifecta

The [lethal trifecta](https://simonwillison.net/2025/Jun/16/lethal-trifecta/) —
private-data access **+** exposure to untrusted content **+** external
communication — is what turns a prompt injection into an exfiltration. Boundary
now touches all three legs:

| Trifecta leg | Boundary today |
|---|---|
| Private-data access | **Partially** — reads are unbounded, but once a run reads untrusted external content, the taint gate (`--on-taint`) treats any subsequent write as a potential exfil channel |
| Untrusted content drives action | **Bounded** — the staging pivot forces a committed thesis before deep reads/writes, and the taint gate refuses/warns when untrusted content flows into a writable sink (the write-as-exfil channel) |
| External communication | **Bounded** — commit-tool gating + write allowlist bound irreversible/outbound actions; with `--sandbox-driver srt` an OS-enforced egress allowlist bounds network exfiltration across the whole process tree (default `seatbelt` driver does not) |

The taint dimension is coarse and **file-granular** — it tracks *which files* are
untrusted, not which bytes. A run is tainted when it fetches external content,
reads a file a prior run marked tainted, or runs `bash` without OS-bounded egress
(`--sandbox-driver srt`); a write made while tainted marks its output tainted
too. The ledger persists per workspace (outside it, so the sandboxed agent can't
clear it), so taint carries **across pipeline stages and separate scheduled
runs** — the stage that finally commits is no longer blind to what an earlier
stage fetched. Default is `warn` (a verdict line, not a block); `refuse` blocks
the write in any run that became tainted. Inspect/clear with `boundary taint`.

The honest gaps: (1) it is file-granular, not per-value — reading a tainted file
taints the whole run even if no tainted byte reaches the output (an
over-approximation, in the safe direction); (2) it catches untrusted-content →
**write/commit** sinks, not exfil through a second `fetch_url` (data in a URL is
an external *read*, not a gated sink) — that channel is closed only by `srt`'s OS
egress allowlist, and a tainted run without `srt` is flagged `egress_uncontained`
(a Third Umpire failure). Per-value information-flow tracking remains future work.

### The six secure-agent design patterns

Against the [six design patterns for securing LLM agents](https://arxiv.org/abs/2506.08837),
Boundary is mainly a **Plan-Then-Execute** system — the staging pivot is its
"commit a plan before acting" — with **Action-Selector** typed commit tools for
irreversible actions. The twist those patterns don't have: a *post-run* check
(the Third Umpire) that the plan actually held. Boundary does not implement
Dual-LLM or Context-Minimization isolation; those stay available as overlays if
coarse controls prove insufficient. A fixed plan protects action *choice*, not
action *parameters* — Boundary inherits that limit and names it.

### How this differs from neighbors

Neighbor characterizations are from the
[coding-agent sandbox census](https://gist.github.com/wincent/2752d8d97727577050c043e4ff9e386e).

| Project | Category | Boundary's difference |
|---|---|---|
| **predicate-secure** | Policy authz + post-run verification (closest sibling) | Same shape, plus the **staging pivot**: a mid-run gate that makes a refused write resume from a staged thesis instead of restarting research |
| **Cupcake** | OPA/Rego policy hooks on tool calls | Boundary's authz is a typed envelope (write allowlist + commit policy), not a general policy engine — narrower, but the Third Umpire *grades whether the envelope held*, which a hook layer doesn't |
| **nah** | allow / ask / block guard on commands | Same allow/ask/refuse verbs, but attached to typed tool *kinds* and a write-count budget, with post-run grading on top |

**The primitive none of them have is the staging pivot** — forcing the agent to
stage a provisional answer mid-run, then resuming a refused write from that stage
rather than from scratch. That is Boundary's differentiator.

## Read the guide

**[GUIDE.md](GUIDE.md)** — operational manual. Setup, all three modes, schedule syntax, cost knobs, troubleshooting. Read this first.

## Examples

**[examples/README.md](examples/README.md)** has runnable starter recipes:

- Prompt files for research, repo review, doc maintenance, and release notes
- A tiny sample workspace you can safely let agents inspect and write into
- Schedule YAMLs for daily/weekly headless runs
- Pipeline YAMLs for multi-persona runs
- A sample overlay that maps role names to local prompt files

## Install

Public alpha — pin to a tag once you have a workflow you like.

```bash
# Recommended: isolated install via pipx
pipx install git+https://github.com/mavaali/boundary.git

# Or in a venv
python3 -m venv .venv && source .venv/bin/activate
pip install git+https://github.com/mavaali/boundary.git
```

Requires Python 3.10+. After install, authenticate the Copilot backend once:

```bash
boundary copilot login           # device-code flow, opens https://github.com/login/device
boundary copilot status          # should say "oauth token: present"
```

For local development (clone + editable install), see [GUIDE.md](GUIDE.md#contributor-setup-editable-install).

## Quick start

```bash
boundary copilot login           # first-time only

boundary run \
  --system-file examples/prompts/researcher.md \
  --workspace examples/workspaces/sample-repo \
  --envelope-writable "scratch/snapshot-$(date +%F).md" \
  --envelope-min-writes 1 --envelope-max-writes 3 \
  --max-iters 25 \
  --task "Summarize the repo and identify one improvement." --verbose
```

> The `examples/` tree ships with the package. Copy it next to your working
> directory or pass absolute paths if you installed via `pipx`.

## Doctrine

Built on the principles in [Hallucinated Intent and the Envelope Problem](https://www.waglesworld.com/blog/hallucinated-intent-and-the-envelope-problem). The agent is not an employee. The envelope is the game plan. The Fielding Coach sets it before play; the Third Umpire checks whether it held afterward.
