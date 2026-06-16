# Boundary

[![selftest](https://github.com/mavaali/boundary/actions/workflows/selftest.yml/badge.svg)](https://github.com/mavaali/boundary/actions/workflows/selftest.yml)

**Agents do not need more trust. They need a boundary.**

Boundary runs tool-calling agents inside an explicit envelope: what they may
read, what they may write, when they must stage a thesis, and how the run gets
reviewed afterward. It is for the moment when a coding agent is useful enough to
delegate to, but not safe enough to leave unsupervised.

**Three modes:**
- Interactive: `boundary run --system-file <prompt.md> --task "..."`
- Fielding Coach: `boundary fielding-coach "loose prompt" --workspace <dir>`
- Scheduled: `boundary schedule install <yaml>` (macOS launchd)

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

- `seatbelt` (default) — macOS write-jail; blocks local writes outside the
  workspace, but **does not bound network egress** and reads are unrestricted.
- `srt` — [Anthropic sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime)
  (Seatbelt/bubblewrap/WFP) adds an **OS-enforced network egress allowlist** over
  the whole process tree. Set `--egress-allow <domain>` (empty = no network);
  needs `npm i -g @anthropic-ai/sandbox-runtime`.
- `none` — no sandbox.

For sensitive work, prefer `--sandbox-driver srt` with a tight egress allowlist
(or run as a dedicated OS user / inside a container), and disable shell or web
tools when they are not needed.

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

The taint dimension is coarse (run-level): once *any* untrusted source is read,
writes are flagged — it does not track which bytes flowed where. Default is
`warn` (a verdict line, not a block); `refuse` blocks all writes post-taint.
Per-value / per-sink granularity is future work.

The honest gap: an allowlisted write is itself an exfiltration channel if its
content is tainted. Closing it is information-flow tracking — on the roadmap, not
shipped.

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
- A sample overlay that maps role names to local prompt files

## Quick start

```bash
cd ~/projects/boundary
source .venv/bin/activate
boundary copilot login           # first-time only

boundary run \
  --system-file examples/prompts/researcher.md \
  --workspace examples/workspaces/sample-repo \
  --envelope-writable "scratch/snapshot-$(date +%F).md" \
  --envelope-min-writes 1 --envelope-max-writes 3 \
  --max-iters 25 \
  --task "Summarize the repo and identify one improvement." --verbose
```

## Doctrine

Built on the principles in [Hallucinated Intent and the Envelope Problem](https://www.waglesworld.com/blog/hallucinated-intent-and-the-envelope-problem). The agent is not an employee. The envelope is the game plan. The Fielding Coach sets it before play; the Third Umpire checks whether it held afterward.
