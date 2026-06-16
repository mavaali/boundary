# Boundary examples

These examples are designed to be copied, edited, and run from the repository root.

```bash
cd ~/projects/boundary
source .venv/bin/activate
```

## What is here

| Path | Purpose |
|---|---|
| `prompts/` | Reusable system prompts for common agent roles |
| `schedules/` | Headless schedule YAML templates |
| `overlays/sample/` | A portable overlay that maps role names to prompts |
| `workspaces/sample-repo/` | A tiny safe workspace for first runs |
| `hello_world.py` | Minimal Python API smoke example |

## 1. First envelope run

This uses the sample workspace and writes only into its `scratch/` directory.

```bash
boundary run \
  --system-file examples/prompts/researcher.md \
  --workspace examples/workspaces/sample-repo \
  --envelope-writable "scratch/research-snapshot.md" \
  --envelope-min-writes 1 \
  --envelope-max-writes 1 \
  --envelope-max-unstaged-reads 2 \
  --envelope-max-dollars 0.10 \
  --max-iters 12 \
  --task "Read the README and docs/product-notes.md. Stage a thesis, then write a concise repo summary with one improvement." \
  --verbose
```

Then grade the transcript printed at the end:

```bash
boundary third-umpire ~/.boundary/transcripts/<transcript>.jsonl
```

## 2. Role-based run through an overlay

The sample overlay resolves role prompt paths relative to `examples/overlays/sample/overlay.yaml`.

```bash
boundary overlays show sample

boundary run \
  --overlay sample \
  --role repo-reviewer \
  --envelope-writable "scratch/repo-review.md" \
  --envelope-min-writes 1 \
  --envelope-max-writes 1 \
  --envelope-max-dollars 0.10 \
  --max-iters 14 \
  --task "Review the sample repo for one correctness risk and one maintainability risk." \
  --verbose
```

## 3. Fielding Coach

Use this when the task is loose and you want the planner to propose the envelope.

```bash
boundary fielding-coach \
  "review the sample repo and write a short risk brief under scratch/" \
  --workspace examples/workspaces/sample-repo
```

Use `--auto` only when you are comfortable skipping the proposal approval gate.

## 4. Schedules

Validate every bundled schedule:

```bash
for f in examples/schedules/*.yaml; do
  echo "== $f =="
  boundary schedule validate "$f"
done
```

Run one schedule immediately without installing launchd:

```bash
boundary schedule-run examples/schedules/daily-docs-check.yaml --verbose
```

Install only after editing `workspace`, `persona`, caps, and notification policy for your repo:

```bash
boundary schedule install examples/schedules/weekly-coverage.yaml
```

## 5. Python API

```bash
python examples/hello_world.py
```

Use the CLI examples first. The Python API is best when embedding Boundary in another tool.

