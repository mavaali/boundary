"""STATE — human-readable, compounding loop state per workspace.

The loop forgets between runs; the repository does not. This module renders a
STATE.md from the History ledger that answers the three questions a loop's state
must answer (per the loop-engineering anatomy, part 6 "Memory"):

  1. What are we working on right now?
  2. What did we try last time and what happened?
  3. What is waiting for a human?

It is read-only over History — a projection, never a second source of truth.
History stays the ledger; STATE.md is the at-a-glance compounding artifact a
human (or the next loop run) reads first.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
from pathlib import Path

from boundary.history import History

STATE_FILENAME = "STATE.md"


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _verdict_pill(v: str | None) -> str:
    return {"PASS": "🟢 PASS", "WARN": "🟡 WARN", "FAIL": "🔴 FAIL"}.get(v or "", "⚪ —")


def render_state(workspace: str, history: History, *, last_n: int = 5) -> str:
    """Render STATE.md markdown for one workspace from the History ledger."""
    ws = str(Path(workspace).expanduser())
    runs = history.runs_for_workspace(ws, limit=last_n)
    reviews = [r for r in history.list_open_reviews() if _review_in_workspace(r, ws)]
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    lines: list[str] = [f"# Loop STATE — `{ws}`", "", f"_generated {now}_", ""]

    # 1. Now
    lines.append("## Working on now")
    if runs:
        r = runs[0]
        open_run = r["ended_at"] is None
        label = r["schedule_name"] or "(adhoc)"
        verdict = _verdict_pill(r["third_umpire_verdict"])
        if open_run:
            lines.append(f"- **{label}** ({r['persona'] or '-'}) — running since {_fmt_ts(r['started_at'])}")
        else:
            lines.append(f"- last: **{label}** ({r['persona'] or '-'}) — {verdict}, stop=`{r['stop_reason']}` @ {_fmt_ts(r['started_at'])}")
    else:
        lines.append("- nothing yet")
    lines.append("")

    # 2. Last tries
    lines.append("## What we tried last")
    if runs:
        lines.append("| when | run | persona | verdict | stop | writes | $ |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in runs:
            lines.append(
                f"| {_fmt_ts(r['started_at'])} | {r['schedule_name'] or '(adhoc)'} | "
                f"{r['persona'] or '-'} | {_verdict_pill(r['third_umpire_verdict'])} | "
                f"`{r['stop_reason'] or '-'}` | {r['writes_executed'] or 0} | "
                f"{(r['estimated_dollars'] or 0):.4f} |"
            )
    else:
        lines.append("- no runs recorded")
    lines.append("")

    # 3. Waiting for human
    lines.append("## Waiting for a human")
    if reviews:
        for rv in reviews:
            lines.append(f"- #{rv['id']} ({rv['schedule_name'] or '-'}): {rv['question'][:200]}")
            lines.append(f"  - resolve: `boundary review-queue resolve {rv['id']} 'note'`")
    else:
        lines.append("- nothing blocked")
    lines.append("")
    return "\n".join(lines)


def _review_in_workspace(review: dict, ws: str) -> bool:
    tp = review.get("transcript_path") or ""
    sn = review.get("schedule_name") or ""
    return ws in tp or ws.rstrip("/").split("/")[-1] in sn


def write_state(workspace: str, history: History, *, last_n: int = 5) -> Path:
    """Render and write STATE.md into the workspace root. Returns the path."""
    ws = Path(workspace).expanduser()
    md = render_state(str(ws), history, last_n=last_n)
    out = ws / STATE_FILENAME
    out.write_text(md, encoding="utf-8")
    return out
