# agent-kit

A tool-calling agent harness for the Avengers/West Wing/Dream XI personas. Standalone Python, ~2200 LOC, no dependency on Scout.

**Three modes:**
- Interactive: `agent-kit run --persona <charter> --task "..."`
- Captain: `agent-kit stark "loose prompt" --workspace <dir>`
- Scheduled: `agent-kit schedule install <yaml>` (macOS launchd)

Every run is wrapped in an **envelope** (write allowlist, spend caps, ambiguity halt) and auto-graded by **Fury** (property checks against the envelope spec, not against the agent).

## Read the guide

**[GUIDE.md](GUIDE.md)** — operational manual. Setup, all three modes, schedule syntax, cost knobs, troubleshooting. Read this first.

## Quick start

```bash
cd ~/projects/agent-kit
source .venv/bin/activate
agent-kit copilot login           # first-time only

agent-kit run \
  --persona ~/projects/FabricSpecs/.squad/agents/banner/charter.md \
  --workspace ~/projects/FabricSpecs \
  --clawpilot \
  --envelope-writable "scratch/snapshot-$(date +%F).md" \
  --envelope-min-writes 1 --envelope-max-writes 3 \
  --max-iters 25 \
  --task "Your task here" --verbose
```

## Doctrine

Built on the principles in [Hallucinated Intent and the Envelope Problem](https://www.waglesworld.com/blog/hallucinated-intent-and-the-envelope-problem). The agent is not an employee. The envelope is the game plan. Fury is the third umpire.

