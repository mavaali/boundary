"""FAIL -> retighten retry — closes the Decide beat of the loop.

multirun.py already does best-of-K (fan out, gate FAILs, pick winner). This
module does the *sequential* complement: dispatch once, grade, and if the Third
Umpire returns FAIL, tighten the envelope, fold the failed-check detail back
into the task as verifier feedback, and re-dispatch — up to max_attempts. A
verifier verdict that never re-enters the next attempt is a gate, not a loop;
this is what makes the verdict feed forward.

dispatch_fn / grade_fn are injectable so the retighten policy is unit-testable
without a live model.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from boundary.fielding_coach import EnvelopeProposal
from boundary.fielding_coach import dispatch as _default_dispatch
from boundary.third_umpire import ThirdUmpire, ThirdUmpireReport


def _failed_checks(report: ThirdUmpireReport) -> list[str]:
    return [f"{c.name}: {c.detail}" for c in report.checks
            if not c.passed and c.severity == "fail"]


def tighten(proposal: EnvelopeProposal, report: ThirdUmpireReport) -> EnvelopeProposal:
    """Produce a tighter proposal: shrink the write budget toward the minimum and
    fold the Third Umpire's failed checks into the task as explicit feedback."""
    fails = _failed_checks(report)
    feedback = "\n".join(f"- {f}" for f in fails) or "- (no fail-severity detail)"
    new_task = (
        f"{proposal.task}\n\n--- PRIOR ATTEMPT FAILED Third Umpire ---\n"
        f"Fix these before writing:\n{feedback}"
    )
    return replace(
        proposal,
        task=new_task,
        max_writes=max(proposal.min_writes, proposal.max_writes - 1),
    )


@dataclass
class RetryAttempt:
    n: int
    verdict: str
    failed: list[str] = field(default_factory=list)


@dataclass
class RetryResult:
    attempts: list[RetryAttempt]
    final_verdict: str
    final_proposal: EnvelopeProposal
    last_run: Any = None


def dispatch_with_retry(
    proposal: EnvelopeProposal,
    workspace,
    *,
    max_attempts: int = 2,
    dispatch_fn: Callable = _default_dispatch,
    grade_fn: Callable[[Any], ThirdUmpireReport] | None = None,
    **dispatch_kwargs,
) -> RetryResult:
    """Dispatch; on FAIL, tighten + re-dispatch up to max_attempts. PASS/WARN stop."""
    grade_fn = grade_fn or _grade_latest
    attempts: list[RetryAttempt] = []
    cur = proposal
    last = None
    for n in range(1, max_attempts + 1):
        last = dispatch_fn(cur, workspace, **dispatch_kwargs)
        report = grade_fn(last)
        verdict = report.verdict
        attempts.append(RetryAttempt(n=n, verdict=verdict, failed=_failed_checks(report)))
        if verdict != "FAIL":
            return RetryResult(attempts, verdict, cur, last)
        if n < max_attempts:
            cur = tighten(cur, report)
    return RetryResult(attempts, "FAIL", cur, last)


def _grade_latest(_run) -> ThirdUmpireReport:
    from pathlib import Path
    tx_dir = Path.home() / ".boundary" / "transcripts"
    latest = max(tx_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return ThirdUmpire.grade(latest)
