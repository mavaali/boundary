"""Tests for the capability-tax benchmark (benchmarks/capability_tax.py).

A scripted mock caller solves each task two ways: natively (direct file
writes) and through an in-process envelope-enforced Gateway. Verifies the
scoring core detects success from the workspace, that gateway-mode work
actually rides the envelope (a sloppy caller writing off-allowlist FAILS in
gateway mode while passing natively — the exact shape a real tax case takes),
and that the report renders the deltas. No model, no `mcp` package.
"""
from __future__ import annotations

from pathlib import Path

from benchmarks.capability_tax import (
    TASKS,
    inprocess_gateway_ctx,
    report_markdown,
    score_pair,
)

TASK = {t.id: t for t in TASKS}

# Deterministic solutions, one per task, as (relative path, content).
SOLUTIONS = {
    "precise_edit": ("src/greeting.py", 'def greet(name):\n    return f"Hello, {name}!"\n'),
    "synthesize_report": ("out/report.md", "Total tickets: 412. Top churn driver: renewal friction.\n"),
    "search_and_count": ("out/answer.txt", "3\n"),
}


def _competent_caller(task, workspace: Path, gateway_ctx):
    rel, content = SOLUTIONS[task.id]
    if gateway_ctx is None:
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    else:
        result = gateway_ctx["gateway"].call(
            "boundary_write_file",
            {"path": rel, "content": content, "reason": "solve the benchmark task"},
        )
        assert not result.startswith("ENVELOPE REFUSED"), result
    return {"cost_usd": 0.01 if gateway_ctx else 0.02, "turns": 3}


def _sloppy_caller(task, workspace: Path, gateway_ctx):
    """Writes the right content to the WRONG place — natively that still may
    land anywhere; through the gateway the allowlist refuses it."""
    rel, content = SOLUTIONS[task.id]
    if gateway_ctx is None:
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    else:
        gateway_ctx["gateway"].call(
            "boundary_write_file",
            {"path": "elsewhere/" + Path(rel).name, "content": content, "reason": "r"},
        )
    return None


def test_competent_caller_pays_no_success_tax(tmp_path):
    rows = [score_pair(t, _competent_caller, tmp_path, inprocess_gateway_ctx)
            for t in TASKS]
    assert all(r.native.success for r in rows)
    assert all(r.gateway.success for r in rows)
    assert all(r.native.wall_seconds >= 0 for r in rows)


def test_sloppy_gateway_use_is_scored_as_tax(tmp_path):
    row = score_pair(TASK["precise_edit"], _sloppy_caller, tmp_path,
                     inprocess_gateway_ctx)
    assert row.native.success is True
    assert row.gateway.success is False  # allowlist refusal → task not done


def test_caller_exception_scores_fail_not_crash(tmp_path):
    def exploding(task, workspace, gateway_ctx):
        raise RuntimeError("caller died")
    row = score_pair(TASK["search_and_count"], exploding, tmp_path,
                     inprocess_gateway_ctx)
    assert row.native.success is False
    assert "caller died" in row.native.error


def test_success_never_trusted_from_caller_metadata(tmp_path):
    def liar(task, workspace, gateway_ctx):
        return {"cost_usd": 0.0, "turns": 1}  # claims nothing, writes nothing
    row = score_pair(TASK["synthesize_report"], liar, tmp_path,
                     inprocess_gateway_ctx)
    assert row.native.success is False
    assert row.gateway.success is False


def test_report_markdown_shows_tax_and_multiplier(tmp_path):
    rows = [score_pair(t, _competent_caller, tmp_path / "a", inprocess_gateway_ctx)
            for t in TASKS]
    rows.append(score_pair(TASK["precise_edit"], _sloppy_caller, tmp_path / "b",
                           inprocess_gateway_ctx))
    report = report_markdown(rows, caller_name="mock")
    assert "Tax cases" in report and "precise_edit" in report
    assert "cost multiplier" in report
    assert report.count("| precise_edit |") == 2
