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
| `schedules/` | Headless schedule YAML templates (see `spend-controlled-daily.yaml` for the full spend + per-tenant chargeback surface) |
| `pipelines/` | Multi-persona pipeline YAML templates with a squad-planning gate |
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

## 5. Pipelines

Pipelines add one shared squad-planning run before the individual persona runs.
The planner writes a squad plan, Third Umpire grades it, and if the plan does
not fail, each downstream persona receives it before its own enforced
`stage_proposal`.

```bash
boundary pipeline validate examples/pipelines/squad-docs-health.yaml
boundary pipeline-run examples/pipelines/squad-docs-health.yaml --verbose
```

Install only after editing `workspace`, personas, writable paths, and caps:

```bash
boundary pipeline install examples/pipelines/squad-docs-health.yaml
```

## 6. Spend control & chargeback

`schedules/spend-controlled-daily.yaml` exercises the full spend surface in one
file: per-run `max_dollars`, fail-closed pricing (`on_unpriced_model`), the spend
gradient, degrade-to-cheaper, a cross-run `budget:` scoped per tenant, and
`attribution:` tags stamped on every run.

```bash
boundary schedule validate examples/schedules/spend-controlled-daily.yaml
boundary schedule-run examples/schedules/spend-controlled-daily.yaml --verbose
```

The tags turn spend into a **chargeback** loop — attribute, run, then read the
bill. Deploy the schedule once per tenant (vary `attribution.tenant`, e.g. `acme`,
`globex`); every run is stamped and lands in the shared ledger.

```bash
# Per-tenant cap status (from the YAML's own budget: block):
boundary budget examples/schedules/spend-controlled-daily.yaml

# The bill — total spend grouped by tenant across every run:
boundary history --by tenant
#   spend by tenant (all time):
#     acme                     $   0.5500      2 run(s)
#     globex                   $   0.2800      1 run(s)
#     -----------------------
#     total                    $   0.8300      3 run(s)

# Window it to the current billing period:
boundary history --by tenant --since 30
```

`--by` works for any attribution key (`--by project`, `--by purpose`). The budget
*bounds* one tenant's spend; the rollup *reports* every tenant's — the two halves
of the enforced-cost envelope.

## 7. Python API

```bash
python examples/hello_world.py
```

Use the CLI examples first. The Python API is best when embedding Boundary in another tool.
