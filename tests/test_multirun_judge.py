"""Checkpoint-2 tests for best-of-K: bounded judge, mode-aware resolution,
review-queue escalation."""
from __future__ import annotations

from types import SimpleNamespace

from boundary.agent import Agent
from boundary.envelope import Envelope
from boundary.history import History
from boundary.multirun import (
    Candidate,
    JudgeVerdict,
    judge_candidates,
    resolve_selection,
    run_best_of_k,
)
from boundary.transcript import Transcript


class _JudgeClient:
    model = "judge"

    def __init__(self, ranking, margin, abstain=False):
        self.ranking = ranking  # list of (run, score, rationale)
        self.margin = margin
        self.abstain = abstain

    def chat(self, messages, tools=None, tool_choice=None, **kw):
        from boundary.clients.base import ChatResponse, Message, ToolCall
        args = {
            "ranking": [{"run": r, "score": s, "rationale": why} for (r, s, why) in self.ranking],
            "margin": self.margin,
            "abstain": self.abstain,
        }
        tc = ToolCall(id="j1", name="emit_ranking", arguments=args)
        return ChatResponse(message=Message(role="assistant", content="", tool_calls=[tc]),
                            finish_reason="tool_calls", input_tokens=1, output_tokens=1, cached_input_tokens=0)


class _NoToolJudge:
    model = "judge"

    def chat(self, messages, tools=None, tool_choice=None, **kw):
        from boundary.clients.base import ChatResponse, Message
        return ChatResponse(message=Message(role="assistant", content="dunno"),
                            finish_reason="stop", input_tokens=1, output_tokens=1, cached_input_tokens=0)


def _cand_with_file(tmp_path, k, body):
    rp = f"out-run{k}.md"
    (tmp_path / rp).write_text(body)
    res = SimpleNamespace(results_by_class={"success": 1})
    return Candidate(k=k, run_paths={rp: "out.md"}, result=res, verdict="PASS", transcript_path="")


def test_judge_parses_ranking(tmp_path):
    pool = [_cand_with_file(tmp_path, 1, "weak"), _cand_with_file(tmp_path, 2, "strong")]
    jc = _JudgeClient([(2, 0.9, "best"), (1, 0.4, "weaker")], margin=0.5)
    v = judge_candidates(jc, "task", pool, tmp_path)
    assert v.top == 2
    assert v.ranking == [2, 1]
    assert v.margin == 0.5
    assert v.abstain is False


def test_judge_no_tool_abstains(tmp_path):
    pool = [_cand_with_file(tmp_path, 1, "a"), _cand_with_file(tmp_path, 2, "b")]
    v = judge_candidates(_NoToolJudge(), "task", pool, tmp_path)
    assert v.abstain is True
    assert set(v.ranking) == {1, 2}


def _v(ranking, margin, abstain=False):
    return JudgeVerdict(ranking=ranking, margin=margin, abstain=abstain)


def test_resolve_clear_promotes():
    r = resolve_selection(_v([2, 1], 0.5), mode="interactive", margin_threshold=0.15,
                          headless_fallback="auto_pick_flag", all_failed=False)
    assert r.winner_k == 2 and r.promote and r.escalation == "none"


def test_resolve_close_interactive_blocks():
    r = resolve_selection(_v([2, 1], 0.05), mode="interactive", margin_threshold=0.15,
                          headless_fallback="auto_pick_flag", all_failed=False)
    assert r.winner_k == 2 and not r.promote and r.escalation == "ratify"


def test_resolve_close_headless_autopick():
    r = resolve_selection(_v([2, 1], 0.05), mode="headless", margin_threshold=0.15,
                          headless_fallback="auto_pick_flag", all_failed=False)
    assert r.winner_k == 2 and r.promote and r.escalation == "advisory"


def test_resolve_close_headless_defer():
    r = resolve_selection(_v([2, 1], 0.05), mode="headless", margin_threshold=0.15,
                          headless_fallback="defer", all_failed=False)
    assert r.winner_k is None and not r.promote and r.escalation == "advisory_defer"


def test_resolve_abstain_escalates():
    r = resolve_selection(_v([1, 2], 0.9, abstain=True), mode="interactive",
                          margin_threshold=0.15, headless_fallback="auto_pick_flag", all_failed=False)
    assert r.escalation == "ratify" and not r.promote


def test_resolve_all_failed_escalates_even_with_margin():
    r = resolve_selection(_v([1, 2], 0.9), mode="headless", margin_threshold=0.15,
                          headless_fallback="auto_pick_flag", all_failed=True)
    assert r.escalation == "advisory" and r.promote


def test_resolve_empty_ranking():
    r = resolve_selection(_v([], 0.0), mode="interactive", margin_threshold=0.15,
                          headless_fallback="auto_pick_flag", all_failed=True)
    assert r.winner_k is None and r.escalation == "ratify"


# --- integration ---

class _ScriptClient:
    model = "claude-sonnet-4.6"

    def __init__(self, script):
        self.script = list(script); self.i = 0

    def chat(self, messages, tools=None, **kw):
        from boundary.clients.base import ChatResponse, Message, ToolCall
        if self.i >= len(self.script):
            return ChatResponse(message=Message(role="assistant", content="done"),
                                finish_reason="stop", input_tokens=1, output_tokens=1, cached_input_tokens=0)
        name, args = self.script[self.i]; self.i += 1
        tc = ToolCall(id=f"c{self.i}", name=name, arguments=args)
        return ChatResponse(message=Message(role="assistant", content="", tool_calls=[tc]),
                            finish_reason="tool_calls", input_tokens=1, output_tokens=1, cached_input_tokens=0)


def _factory(tmp_path):
    def f(run: int) -> Agent:
        path = f"out-run{run}.md"
        client = _ScriptClient([("write_file", {"path": path, "content": f"content{run}", "reason": "t"})])
        return Agent(name=f"r{run}", system_prompt="x", workspace=str(tmp_path), client=client,
                     enable_fs=True, enable_shell=False, enable_web=False,
                     transcript=Transcript(path=tmp_path / f"t-{run}.jsonl"))
    return f


def _base():
    return Envelope(writable_paths=["out.md"], require_staging=False, repeat_halt=0)


def test_integration_clear_margin_promotes(tmp_path):
    res = run_best_of_k(
        agent_factory=_factory(tmp_path), base_envelope=_base(), task="do it",
        workspace_root=tmp_path, k=2,
        judge_client=_JudgeClient([(2, 0.9, "best"), (1, 0.3, "weak")], margin=0.6),
        mode="interactive",
    )
    assert res.escalation == "none"
    assert res.winner.k == 2
    assert (tmp_path / "out.md").read_text() == "content2"
    assert res.review_id is None


def test_integration_close_interactive_blocks_and_queues(tmp_path):
    hist = History(db_path=tmp_path / "h.db")
    res = run_best_of_k(
        agent_factory=_factory(tmp_path), base_envelope=_base(), task="do it",
        workspace_root=tmp_path, k=2,
        judge_client=_JudgeClient([(2, 0.55, "a"), (1, 0.50, "b")], margin=0.05),
        mode="interactive", history=hist,
    )
    assert res.escalation == "ratify"
    assert not (tmp_path / "out.md").exists()  # nothing promoted; awaiting ratify
    assert res.review_id is not None
    assert len(hist.list_open_reviews()) == 1
    hist.close()


def test_integration_close_headless_autopicks_and_flags(tmp_path):
    hist = History(db_path=tmp_path / "h.db")
    res = run_best_of_k(
        agent_factory=_factory(tmp_path), base_envelope=_base(), task="do it",
        workspace_root=tmp_path, k=2,
        judge_client=_JudgeClient([(2, 0.55, "a"), (1, 0.50, "b")], margin=0.05),
        mode="headless", headless_fallback="auto_pick_flag", history=hist,
    )
    assert res.escalation == "advisory"
    assert (tmp_path / "out.md").read_text() == "content2"  # auto-picked, promoted
    assert res.review_id is not None  # advisory flag queued, non-blocking
    hist.close()
