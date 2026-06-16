# Boundary

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

Backwards-compatible local aliases remain available: `stark` for `fielding-coach`, and `fury` for `third-umpire`.
The legacy `agent-kit` command is also kept as a compatibility alias for `boundary`.

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

Boundary enforces a workspace write boundary and envelope write allowlist. The
macOS shell wrapper blocks local writes outside the workspace, but it is not a
complete sandbox: shell commands may still read files allowed by the operating
system user, and network egress is not fully blocked. For sensitive work, run
Boundary as a dedicated OS user or inside a container, and disable shell or web
tools when they are not needed.

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
