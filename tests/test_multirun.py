"""Checkpoint-1 tests for best-of-K (multirun): templating, gate, stub-select,
promotion, fan-out, and History summary."""
from __future__ import annotations

from types import SimpleNamespace

from boundary.agent import Agent
from boundary.envelope import Envelope
from boundary.history import History
from boundary.transcript import Transcript
from boundary.multirun import (
    Candidate, template_run_paths, gate_survivors, stub_select,
    promote_winner, run_best_of_k, record_best_of_k,
)


def test_template_run_paths_literal_and_glob():
    m = template_run_paths(["out.md", "scratch/x.md", "logs/*.md"], 2)
    assert m["out-run2.md"] == "out.md"
    assert m["scratch/x-run2.md"] == "scratch/x.md"
    assert m["logs/*.md"] == "logs/*.md"  # glob untouched


def _cand(k, verdict, bad=0):
    res = SimpleNamespace(results_by_class={"success": 5, "arg-invalid": bad})
    return Candidate(k=k, run_paths={}, result=res, verdict=verdict, transcript_path="")


def test_gate_drops_fail():
    cands = [_cand(1, "FAIL"), _cand(2, "PASS"), _cand(3, "WARN")]
    survivors = gate_survivors(cands)
    assert {c.k for c in survivors} == {2, 3}


def test_stub_select_prefers_pass_then_fewer_unproductive():
    # PASS beats WARN regardless of unproductive count.
    assert stub_select([_cand(1, "WARN", bad=0), _cand(2, "PASS", bad=9)]).k == 2
    # Among PASS, fewer unproductive wins; then lowest k.
    pool = [_cand(3, "PASS", bad=4), _cand(1, "PASS", bad=1), _cand(2, "PASS", bad=1)]
    assert stub_select(pool).k == 1


def test_stub_select_empty_pool():
    assert stub_select([]) is None


def test_promote_copies_run_file_to_final(tmp_path):
    (tmp_path / "out-run2.md").write_text("winner body")
    w = Candidate(k=2, run_paths={"out-run2.md": "out.md"}, result=None, verdict="PASS", transcript_path="")
    promoted = promote_winner(w, tmp_path)
    assert promoted == ["out.md"]
    assert (tmp_path / "out.md").read_text() == "winner body"


class _ScriptClient:
    model = "claude-sonnet-4.6"

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def chat(self, messages, tools=None, **kw):
        from boundary.clients.base import ChatResponse, Message, ToolCall
        if self.i >= len(self.script):
            return ChatResponse(message=Message(role="assistant", content="done"),
                                finish_reason="stop", input_tokens=1, output_tokens=1, cached_input_tokens=0)
        name, args = self.script[self.i]; self.i += 1
        tc = ToolCall(id=f"c{self.i}", name=name, arguments=args)
        return ChatResponse(message=Message(role="assistant", content="", tool_calls=[tc]),
                            finish_reason="tool_calls", input_tokens=1, output_tokens=1, cached_input_tokens=0)


def _make_factory(tmp_path):
    def factory(run: int) -> Agent:
        path = f"out-run{run}.md"
        client = _ScriptClient([("write_file", {"path": path, "content": f"content{run}", "reason": "t"})])
        return Agent(
            name=f"r{run}", system_prompt="x", workspace=str(tmp_path), client=client,
            enable_fs=True, enable_shell=False, enable_web=False,
            transcript=Transcript(path=tmp_path / f"transcript-{run}.jsonl"),
        )
    return factory


def test_run_best_of_k_fans_out_and_promotes(tmp_path):
    base = Envelope(writable_paths=["out.md"], require_staging=False, repeat_halt=0)
    res = run_best_of_k(
        agent_factory=_make_factory(tmp_path),
        base_envelope=base,
        task="write the deliverable",
        workspace_root=tmp_path,
        k=3,
    )
    # K isolated candidate files exist, none clobbered.
    assert len(res.candidates) == 3
    for run in (1, 2, 3):
        assert (tmp_path / f"out-run{run}.md").read_text() == f"content{run}"
    # A winner was promoted to the final path.
    assert res.winner is not None
    assert res.promoted == ["out.md"]
    assert (tmp_path / "out.md").exists()
    # The promoted content matches the winning run.
    assert (tmp_path / "out.md").read_text() == f"content{res.winner.k}"


def test_record_best_of_k_logs_summary(tmp_path):
    base = Envelope(writable_paths=["out.md"], require_staging=False, repeat_halt=0)
    res = run_best_of_k(
        agent_factory=_make_factory(tmp_path),
        base_envelope=base,
        task="write it",
        workspace_root=tmp_path,
        k=2,
    )
    hist = History(db_path=tmp_path / "h.db")
    rid = record_best_of_k(hist, res, persona="writer", workspace=str(tmp_path),
                           started_at=1.0, ended_at=2.0)
    assert rid is not None
    runs = hist.list_runs(limit=5)
    assert len(runs) == 1
    assert runs[0]["stop_reason"] == "best_of_k"
    hist.close()
