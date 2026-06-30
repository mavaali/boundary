"""Best-of-K multi-run orchestration (feature C).

Runs K independent EnvelopeRunner dispatches of the same task, each writing to a
per-run templated path so they do not clobber one another, grades each with the
Third Umpire, drops FAIL runs, selects a winner, and promotes the winner's
artifact(s) to the final path(s).

Checkpoint 1 (this module's first cut) ships:
  - template_run_paths  (T1) — per-run output isolation via path templating
  - run_best_of_k       (T2) — the fan-out orchestrator (sequential)
  - gate_survivors      (T3) — drop Third-Umpire FAIL runs from the pool
  - stub_select         (placeholder selector — replaced by the bounded judge
                          + mode-aware resolution in checkpoint 2)
  - promote_winner      (T6) — copy the winner's run file(s) to the final path(s)
  - record_best_of_k    (T9) — log a best-of-K summary run to History

Variance across runs is supplied by the caller via `chat_kwargs_for` (e.g. a
temperature schedule); the orchestrator itself is sampling-agnostic.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeRunner, EnvelopeRunResult
from boundary.third_umpire import ThirdUmpire

_VERDICT_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}


@dataclass
class Candidate:
    k: int
    run_paths: dict           # templated run path -> final path
    result: EnvelopeRunResult | Any
    verdict: str              # PASS | WARN | FAIL
    transcript_path: str
    written: list[str] = field(default_factory=list)  # templated paths that exist


@dataclass
class BestOfKResult:
    candidates: list[Candidate]
    survivors: list[Candidate]
    winner: Candidate | None
    promoted: list[str]
    selection_reason: str
    judge: JudgeVerdict | None = None
    escalation: str = "none"          # none | ratify | advisory | advisory_defer
    review_id: int | None = None

    def summary(self) -> dict:
        return {
            "k": len(self.candidates),
            "verdicts": [c.verdict for c in self.candidates],
            "survivors": [c.k for c in self.survivors],
            "winner": self.winner.k if self.winner else None,
            "promoted": list(self.promoted),
            "escalation": self.escalation,
            "judge_margin": self.judge.margin if self.judge else None,
            "judge_abstain": self.judge.abstain if self.judge else None,
            "selection_reason": self.selection_reason,
        }


def template_run_paths(writable_paths: list[str], k: int) -> dict[str, str]:
    """T1: map each final writable path to a per-run path (stem + '-run{k}').

    'out.md' -> 'out-run1.md'; 'scratch/x.md' -> 'scratch/x-run1.md'. Glob paths
    (containing '*') are left unchanged — best-of-K targets single-artifact,
    literal-path envelopes in v1; multi-file isolation via worktrees is a later
    upgrade.
    """
    mapping: dict[str, str] = {}
    for p in writable_paths:
        if "*" in p:
            mapping[p] = p
            continue
        pp = PurePosixPath(p)
        run_p = str(pp.with_name(f"{pp.stem}-run{k}{pp.suffix}"))
        mapping[run_p] = p
    return mapping


def validate_run_path_isolation(writable_paths: list[str], k: int) -> list[str]:
    """Return reasons the K runs cannot be isolated to distinct output paths.

    Best-of-K only works if each run writes somewhere the others don't; otherwise
    run K silently clobbers run K-1 and the judge compares one survivor against
    itself. `template_run_paths` leaves glob paths unchanged, so a glob writable
    path maps to the same target for every run — the headline collision. We also
    guard the (rare) case where two literal paths template to the same run path.
    """
    problems: list[str] = []
    if k <= 1 or not writable_paths:
        return problems
    for p in writable_paths:
        if "*" in p:
            problems.append(
                f"writable path {p!r} contains a glob and cannot be isolated across "
                f"{k} runs — every run would write the same target and clobber the "
                f"others. Best-of-K needs literal paths (one artifact per run)."
            )
    seen: dict[str, int] = {}
    for run in range(1, k + 1):
        for run_p in template_run_paths(writable_paths, run):
            if "*" in run_p:
                continue  # already reported above
            if run_p in seen and seen[run_p] != run:
                problems.append(
                    f"templated path {run_p!r} collides between run{seen[run_p]} and "
                    f"run{run} — choose writable paths that don't alias once suffixed."
                )
            else:
                seen[run_p] = run
    return problems


def _unproductive(candidate: Candidate) -> int:
    rb = getattr(candidate.result, "results_by_class", None) or {}
    return sum(v for kk, v in rb.items() if kk != "success")


def gate_survivors(candidates: list[Candidate]) -> list[Candidate]:
    """T3: keep runs the Third Umpire did not FAIL."""
    return [c for c in candidates if c.verdict != "FAIL"]


def stub_select(pool: list[Candidate]) -> Candidate | None:
    """Placeholder selector for checkpoint 1: prefer better verdict, then fewer
    unproductive tool results, then lowest run index. Replaced by the bounded
    judge + mode-aware resolution in checkpoint 2.
    """
    if not pool:
        return None
    return sorted(
        pool,
        key=lambda c: (_VERDICT_RANK.get(c.verdict, 3), _unproductive(c), c.k),
    )[0]


def promote_winner(winner: Candidate, workspace_root: str | Path) -> list[str]:
    """T6: copy the winner's run file(s) to the final path(s)."""
    root = Path(workspace_root)
    promoted: list[str] = []
    for run_p, final_p in winner.run_paths.items():
        src = root / run_p
        if not src.exists():
            continue
        dst = root / final_p
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        promoted.append(final_p)
    return promoted


EMIT_RANKING_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_ranking",
        "description": "Emit the candidate ranking best→worst. Call exactly once.",
        "parameters": {
            "type": "object",
            "properties": {
                "ranking": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "run": {"type": "integer"},
                            "score": {"type": "number"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["run", "score", "rationale"],
                    },
                },
                "margin": {"type": "number"},
                "abstain": {"type": "boolean"},
            },
            "required": ["ranking", "margin", "abstain"],
        },
    },
}

JUDGE_SYSTEM = """You are the Selection Judge for a best-of-K agent run. You read \
K candidate artifacts produced for the SAME task and rank them.

- Score each candidate 0.0-1.0 on task fit, completeness, correctness, clarity.
- Give a one-line rationale that CITES something concrete in that candidate.
- Rank best→worst. Set `margin` to the score gap between the top two (0.0 if
  tied; 1.0 if there is only one candidate).
- Set `abstain=true` if you cannot meaningfully separate the candidates or lack
  the basis to judge. Abstaining is correct when the field is genuinely close.

Do NOT reward length. You do not write or edit anything — you only emit the
ranking via `emit_ranking`, exactly once."""


@dataclass
class JudgeVerdict:
    ranking: list[int]                 # run indices, best→worst
    scores: dict[int, float] = field(default_factory=dict)
    rationale: dict[int, str] = field(default_factory=dict)
    margin: float = 0.0
    abstain: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def top(self) -> int | None:
        return self.ranking[0] if self.ranking else None


def judge_candidates(judge_client, task: str, pool: list[Candidate],
                     workspace_root: str | Path, *, max_chars: int = 4000) -> JudgeVerdict:
    """T4: bounded read-only judge. Candidate artifacts are inlined as text and
    the judge is given ONLY the `emit_ranking` structured-output tool — it has no
    file/shell tools and cannot write, so it is read-only by construction.
    """
    from boundary.clients.base import Message
    root = Path(workspace_root)
    blocks = []
    for c in pool:
        texts = []
        for run_p in c.run_paths:
            f = root / run_p
            if f.exists():
                texts.append(f"--- {run_p} ---\n{f.read_text(encoding='utf-8')[:max_chars]}")
        rb = getattr(c.result, "results_by_class", {}) or {}
        body = "\n".join(texts) if texts else "(no artifact written)"
        blocks.append(f"### Candidate run{c.k} (third_umpire={c.verdict}, results={rb})\n{body}")
    user = f"TASK:\n{task}\n\nCANDIDATES ({len(pool)}):\n\n" + "\n\n".join(blocks)

    resp = judge_client.chat(
        [Message(role="system", content=JUDGE_SYSTEM), Message(role="user", content=user)],
        tools=[EMIT_RANKING_TOOL],
        tool_choice={"type": "function", "function": {"name": "emit_ranking"}},
    )
    pool_ks = [c.k for c in pool]
    if not resp.message.tool_calls:
        # No structured output → abstain over the current order.
        return JudgeVerdict(list(pool_ks), {k: 0.0 for k in pool_ks}, {}, 0.0, True, {})
    args = resp.message.tool_calls[0].arguments or {}
    ranked = args.get("ranking") or []
    ks = {c.k for c in pool}
    ranking = [int(r["run"]) for r in ranked if isinstance(r, dict) and r.get("run") in ks]
    scores = {int(r["run"]): float(r.get("score", 0.0)) for r in ranked if isinstance(r, dict) and r.get("run") in ks}
    rationale = {int(r["run"]): str(r.get("rationale", "")) for r in ranked if isinstance(r, dict) and r.get("run") in ks}
    if not ranking:
        ranking = list(pool_ks)
    return JudgeVerdict(ranking, scores, rationale,
                        float(args.get("margin", 0.0)), bool(args.get("abstain", False)), args)


@dataclass
class Resolution:
    winner_k: int | None
    promote: bool
    escalation: str          # none | ratify | advisory | advisory_defer
    reason: str


def resolve_selection(verdict: JudgeVerdict, *, mode: str, margin_threshold: float,
                      headless_fallback: str, all_failed: bool) -> Resolution:
    """T5: mode-aware, non-blocking-for-headless resolution.

    clear margin            → promote winner (escalation none)
    close / abstain, interactive → block to review-queue (ratify; promote False)
    close / abstain, headless    → auto-pick + advisory flag (promote True), or
                                   defer (promote nothing) per headless_fallback
    """
    top = verdict.top
    if top is None:
        return Resolution(None, False, "ratify" if mode == "interactive" else "advisory_defer",
                          "no candidates to select")
    close = verdict.abstain or verdict.margin < margin_threshold or all_failed
    if not close:
        return Resolution(top, True, "none",
                          f"clear margin {verdict.margin:.2f} ≥ {margin_threshold}")
    why = ("abstain" if verdict.abstain
           else "all runs FAILed" if all_failed
           else f"close margin {verdict.margin:.2f} < {margin_threshold}")
    if mode == "interactive":
        return Resolution(top, False, "ratify", f"{why}; interactive → human ratify")
    if headless_fallback == "defer":
        return Resolution(None, False, "advisory_defer", f"{why}; headless defer → nothing promoted")
    return Resolution(top, True, "advisory", f"{why}; headless → auto-pick run{top} + advisory flag")


def _queue_selection_review(history, *, escalation: str, result: BestOfKResult) -> int | None:
    """T7: park a best-of-K selection decision in the review-queue.

    Two kinds, both via the existing queue schema (no migration):
      RATIFY   — blocking (interactive close call): human picks the winner.
      ADVISORY — non-blocking (headless close call): winner already auto-picked;
                 human may override. Losers are retained as run files.
    """
    if history is None:
        return None
    pool = result.survivors or result.candidates
    if escalation == "ratify":
        q = (f"[best-of-K selection: RATIFY] Close call — pick the winner to promote. "
             f"{result.selection_reason}")
        opts = [f"run{c.k}" for c in pool] + ["none"]
        tpath = pool[0].transcript_path if pool else None
    else:  # advisory / advisory_defer
        wk = result.winner.k if result.winner else None
        head = f"auto-picked run{wk}" if wk else "promoted nothing (defer)"
        q = (f"[best-of-K selection: ADVISORY] {head} on a close call; override if needed. "
             f"{result.selection_reason}")
        keep = f"keep run{wk}" if wk else "keep deferred"
        opts = [keep] + [f"override run{c.k}" for c in pool]
        tpath = result.winner.transcript_path if result.winner else (pool[0].transcript_path if pool else None)
    return history.queue_review(schedule_name=None, persona=None, question=q,
                                options=opts, transcript_path=tpath, run_id=None)


def run_best_of_k(
    *,
    agent_factory: Callable[[int], Agent],
    base_envelope: Envelope,
    task: str,
    workspace_root: str | Path,
    k: int = 3,
    chat_kwargs_for: Callable[[int], dict] | None = None,
    judge_client=None,
    mode: str = "interactive",
    select_margin: float = 0.15,
    headless_fallback: str = "auto_pick_flag",
    history=None,
    verbose: bool = False,
) -> BestOfKResult:
    """T2: run K independent dispatches, gate on the Third Umpire, select, promote.

    `agent_factory(run_index)` must return a FRESH Agent with its own transcript
    (grading reads the transcript). `chat_kwargs_for(run_index)` supplies per-run
    sampling variance (e.g. temperature); defaults to none.

    Selection: if `judge_client` is None the stub selector is used (checkpoint-1
    behavior). Otherwise the bounded judge ranks survivors and `mode` /
    `select_margin` / `headless_fallback` drive a mode-aware, non-blocking-for-
    headless resolution, with close calls parked in `history`'s review-queue.
    """
    isolation_problems = validate_run_path_isolation(base_envelope.writable_paths, k)
    if isolation_problems:
        raise ValueError(
            "best-of-K run paths cannot be isolated:\n- " + "\n- ".join(isolation_problems)
        )
    root = Path(workspace_root)
    candidates: list[Candidate] = []
    for run in range(1, k + 1):
        mapping = template_run_paths(base_envelope.writable_paths, run)
        env = replace(base_envelope, writable_paths=list(mapping.keys()))
        agent = agent_factory(run)
        ck = (chat_kwargs_for(run) if chat_kwargs_for else {}) or {}
        try:
            res = EnvelopeRunner(agent, env).run(task, verbose=verbose, **ck)
        finally:
            agent.close()
        tpath = str(agent.transcript.path) if agent.transcript else ""
        verdict = "PASS"
        if tpath:
            try:
                verdict = ThirdUmpire.grade(tpath, env).verdict
            except Exception:
                verdict = "PASS"
        written = [rp for rp in mapping if (root / rp).exists()]
        candidates.append(Candidate(run, mapping, res, verdict, tpath, written))

    survivors = gate_survivors(candidates)
    all_failed = not survivors
    pool = survivors or candidates  # all-FAIL: fall through to the resolver

    # Checkpoint-1 path: no judge → stub selector + unconditional promote.
    if judge_client is None:
        winner = stub_select(pool)
        promoted = promote_winner(winner, root) if winner else []
        reason = (
            (f"stub-select: run{winner.k} ({winner.verdict})"
             + ("" if survivors else " — all runs FAILed, picked best of a bad pool"))
            if winner else "no candidate selected"
        )
        return BestOfKResult(candidates, survivors, winner, promoted, reason)

    # Checkpoint-2 path: bounded judge → mode-aware resolution.
    jverdict = judge_candidates(judge_client, task, pool, root)
    res = resolve_selection(jverdict, mode=mode, margin_threshold=select_margin,
                            headless_fallback=headless_fallback, all_failed=all_failed)
    winner = next((c for c in pool if c.k == res.winner_k), None) if res.winner_k is not None else None
    promoted = promote_winner(winner, root) if (res.promote and winner) else []
    out = BestOfKResult(candidates, survivors, winner, promoted, res.reason,
                        judge=jverdict, escalation=res.escalation)
    if res.escalation != "none":
        out.review_id = _queue_selection_review(history, escalation=res.escalation, result=out)
    return out


def record_best_of_k(
    history,
    result: BestOfKResult,
    *,
    persona: str | None,
    workspace: str | None,
    started_at: float,
    ended_at: float,
) -> int | None:
    """T9: log one best-of-K summary run to History using the winner's numbers."""
    w = result.winner
    if w is None:
        return None
    r = w.result
    return history.record_run(
        schedule_name=None,
        persona=persona,
        workspace=workspace,
        started_at=started_at,
        ended_at=ended_at,
        stop_reason="best_of_k",
        iterations=getattr(r.loop_result, "iterations", 0),
        writes_executed=getattr(r, "writes_executed", 0),
        input_tokens=getattr(r, "input_tokens", 0),
        output_tokens=getattr(r, "output_tokens", 0),
        cached_input_tokens=getattr(r, "cached_input_tokens", 0),
        estimated_dollars=sum(getattr(c.result, "estimated_dollars", 0.0) for c in result.candidates),
        wall_seconds=sum(getattr(c.result, "wall_seconds", 0.0) for c in result.candidates),
        third_umpire_verdict=w.verdict,
        third_umpire_summary=result.summary(),
        transcript_path=w.transcript_path,
        written_files=list(result.promoted),
    )
