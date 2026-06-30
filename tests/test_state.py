import time

from boundary.history import History
from boundary.state import STATE_FILENAME, render_state, write_state


def _hist(tmp_path) -> History:
    return History(db_path=tmp_path / "history.db")


def _seed_run(h: History, ws: str, *, name="nightly", verdict="PASS", ended=True):
    now = time.time()
    return h.record_run(
        schedule_name=name, persona="natasha", workspace=ws,
        started_at=now, ended_at=(now + 5 if ended else None),
        stop_reason="completed", iterations=3, writes_executed=2,
        input_tokens=100, output_tokens=50, cached_input_tokens=0,
        estimated_dollars=0.01, wall_seconds=5.0,
        third_umpire_verdict=verdict, third_umpire_summary={},
        transcript_path=f"{ws}/t.jsonl", written_files=["a.md"],
    )


def test_empty_state(tmp_path):
    h = _hist(tmp_path)
    md = render_state(str(tmp_path / "ws"), h, last_n=5)
    assert "nothing yet" in md
    assert "no runs recorded" in md
    assert "nothing blocked" in md


def test_state_answers_three_questions(tmp_path):
    h = _hist(tmp_path)
    ws = str(tmp_path / "ws")
    _seed_run(h, ws, verdict="PASS")
    _seed_run(h, ws, name="retry", verdict="FAIL")
    h.queue_review(schedule_name="nightly", persona="natasha", question="ambiguous spec",
                   options=["a", "b"], transcript_path=f"{ws}/t.jsonl", run_id=1)
    md = render_state(ws, h, last_n=5)
    assert "Working on now" in md
    assert "What we tried last" in md and "FAIL" in md and "PASS" in md
    assert "ambiguous spec" in md and "review-queue resolve" in md


def test_workspace_isolation(tmp_path):
    h = _hist(tmp_path)
    _seed_run(h, str(tmp_path / "a"), name="alpha")
    md = render_state(str(tmp_path / "b"), h, last_n=5)
    assert "alpha" not in md and "nothing yet" in md


def test_write_state_file(tmp_path):
    h = _hist(tmp_path)
    ws = tmp_path / "ws"; ws.mkdir()
    _seed_run(h, str(ws))
    out = write_state(str(ws), h)
    assert out == ws / STATE_FILENAME and out.exists()
    assert "Loop STATE" in out.read_text()
