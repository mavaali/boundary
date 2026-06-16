# Sample overlay

Run from the repository root:

```bash
boundary overlays show sample

boundary run \
  --overlay sample \
  --role docs-maintainer \
  --envelope-writable "scratch/docs-check.md" \
  --task "Inspect the sample repo docs and write one suggested improvement."
```

Copy this directory to `~/.boundary/overlays/<name>/` when you want a persistent local overlay.

