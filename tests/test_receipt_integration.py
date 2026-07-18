"""Receipt integration — transcript logging, history storage, and CLI."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeRunner
from boundary.history import History
from boundary.receipt import Receipt, verify_receipt
from boundary.third_umpire import ThirdUmpire
from boundary.transcript import Transcript


class _ScriptClient:
    model = "claude-haiku-4.5"

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def chat(self, messages, tools=None, **kw):
        from boundary.clients.base import ChatResponse, Message, ToolCall
        if self.i >= len(self.script):
            return ChatResponse(message=Message(role="assistant", content="[DATA] done"),
                                finish_reason="stop", input_tokens=1, output_tokens=1,
                                cached_input_tokens=0)
        name, args = self.script[self.i]; self.i += 1
        tc = ToolCall(id=f"c{self.i}", name=name, arguments=args)
        return ChatResponse(message=Message(role="assistant", content="", tool_calls=[tc]),
                            finish_reason="tool_calls", input_tokens=1, output_tokens=1,
                            cached_input_tokens=0)


def test_envelope_start_records_spec_and_hash(tmp_path):
    """A run's transcript must carry the policy so a receipt can reconstruct and
    a verifier can cross-check it."""
    tp = tmp_path / "t.jsonl"
    client = _ScriptClient([("write_file", {"path": "out.md", "content": "x", "reason": "r"})])
    agent = Agent(name="a", system_prompt="x", workspace=str(tmp_path), client=client,
                  enable_fs=True, enable_shell=False, enable_web=False,
                  transcript=Transcript(path=tp), max_iters=3)
    env = Envelope(writable_paths=["out.md"], require_staging=False, repeat_halt=0)
    EnvelopeRunner(agent, env).run("task")
    agent.close()

    events = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
    start = next(e for e in events if e.get("type") == "envelope_start")
    assert start["spec_hash"] == env.spec_hash()
    assert start["spec"] == env.spec_dict()


def _receipt_for(env: Envelope, tmp_path: Path) -> Receipt:
    events = [
        {"type": "envelope_start", "writable_paths": env.writable_paths,
         "require_staging": env.require_staging,
         "spec": env.spec_dict(), "spec_hash": env.spec_hash(), "task": "t"},
        {"type": "envelope_end", "writes_executed": 1, "min_writes": env.min_writes,
         "on_commit": env.on_commit, "on_taint": env.on_taint,
         "model": "claude-haiku-4.5", "estimated_dollars": 0.02, "events": []},
        {"type": "end", "iterations": 2},
    ]
    tp = tmp_path / "t.jsonl"
    tp.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    report = ThirdUmpire.grade(tp)
    return Receipt.build(report, spec=env.spec_dict(), spec_hash=env.spec_hash(),
                         run_id=None, schedule_name="wiki", model="claude-haiku-4.5",
                         estimated_dollars=0.02, transcript_path=str(tp), created_at=1)


def test_history_stores_and_reads_receipt(tmp_path):
    h = History(tmp_path / "h.db")
    env = Envelope(writable_paths=["out.md"])
    receipt = _receipt_for(env, tmp_path)
    run_id = h.record_run(
        schedule_name="wiki", persona="fury", workspace=str(tmp_path),
        started_at=1.0, ended_at=2.0, stop_reason="stop", iterations=2,
        writes_executed=1, input_tokens=1, output_tokens=1, cached_input_tokens=0,
        estimated_dollars=0.02, wall_seconds=1.0, third_umpire_verdict="PASS",
        third_umpire_summary={}, transcript_path=str(tmp_path / "t.jsonl"),
        written_files=[], receipt=receipt.as_dict(),
    )
    got = h.get_receipt(run_id)
    assert got is not None
    assert got["spec_hash"] == env.spec_hash()
    assert verify_receipt(Receipt.from_dict(got)).ok
    h.close()


def test_history_get_receipt_none_when_absent(tmp_path):
    h = History(tmp_path / "h.db")
    run_id = h.record_run(
        schedule_name=None, persona=None, workspace=str(tmp_path),
        started_at=1.0, ended_at=2.0, stop_reason="stop", iterations=1,
        writes_executed=0, input_tokens=0, output_tokens=0, cached_input_tokens=0,
        estimated_dollars=0.0, wall_seconds=0.0, third_umpire_verdict=None,
        third_umpire_summary=None, transcript_path=None, written_files=[],
    )
    assert h.get_receipt(run_id) is None
    h.close()


def test_cli_receipt_show_and_verify(tmp_path):
    db = tmp_path / "h.db"
    h = History(db)
    env = Envelope(writable_paths=["out.md"])
    receipt = _receipt_for(env, tmp_path)
    run_id = h.record_run(
        schedule_name="wiki", persona="fury", workspace=str(tmp_path),
        started_at=1.0, ended_at=2.0, stop_reason="stop", iterations=2,
        writes_executed=1, input_tokens=1, output_tokens=1, cached_input_tokens=0,
        estimated_dollars=0.02, wall_seconds=1.0, third_umpire_verdict="PASS",
        third_umpire_summary={}, transcript_path=str(tmp_path / "t.jsonl"),
        written_files=[], receipt=receipt.as_dict(),
    )
    h.close()

    show = subprocess.run(
        [sys.executable, "-m", "boundary.cli", "receipt", "show", str(run_id), "--db", str(db)],
        capture_output=True, text=True)
    assert show.returncode == 0, show.stderr
    assert "boundary.receipt/v1" in show.stdout
    assert env.spec_hash() in show.stdout

    verify = subprocess.run(
        [sys.executable, "-m", "boundary.cli", "receipt", "verify", str(run_id), "--db", str(db)],
        capture_output=True, text=True)
    assert verify.returncode == 0, verify.stderr
    assert "OK" in verify.stdout or "ok" in verify.stdout


def test_cli_receipt_verify_fails_on_tamper(tmp_path):
    db = tmp_path / "h.db"
    h = History(db)
    env = Envelope(writable_paths=["out.md"])
    receipt = _receipt_for(env, tmp_path)
    tampered = receipt.as_dict()
    tampered["spec"]["writable_paths"] = ["**"]  # widen policy, keep the hash
    run_id = h.record_run(
        schedule_name="wiki", persona="fury", workspace=str(tmp_path),
        started_at=1.0, ended_at=2.0, stop_reason="stop", iterations=2,
        writes_executed=1, input_tokens=1, output_tokens=1, cached_input_tokens=0,
        estimated_dollars=0.02, wall_seconds=1.0, third_umpire_verdict="PASS",
        third_umpire_summary={}, transcript_path=str(tmp_path / "t.jsonl"),
        written_files=[], receipt=tampered,
    )
    h.close()
    verify = subprocess.run(
        [sys.executable, "-m", "boundary.cli", "receipt", "verify", str(run_id), "--db", str(db)],
        capture_output=True, text=True)
    assert verify.returncode != 0
    assert "MISMATCH" in verify.stdout or "mismatch" in verify.stdout.lower()


def test_scout_hook_event_carries_receipt_hash(tmp_path, monkeypatch):
    from boundary import headless
    from boundary.schedule import ScheduleConfig

    event_dir = tmp_path / "events" / "pending"
    monkeypatch.setattr(headless, "EVENT_PENDING_DIR", event_dir)
    env = Envelope(writable_paths=["out.md"])
    receipt = _receipt_for(env, tmp_path)

    path = headless._emit_scout_hook_event(
        ScheduleConfig(name="wiki", schedule="daily 09:00", persona="fury",
                       workspace=str(tmp_path), task="audit", writable_paths=["out.md"],
                       notify={"scout_hook": {"on": "always"}}),
        run_id=5, review_id=None, stop_reason="stop", third_umpire_verdict="PASS",
        transcript_path=None, written_files=[], error_text=None, rendered_paths=[],
        wall_seconds=1.0, estimated_dollars=0.02, receipt=receipt.as_dict(),
    )
    assert path is not None
    event = json.loads((event_dir / "wiki-5.json").read_text())
    assert event["receipt"]["spec_hash"] == env.spec_hash()


def test_selftest_receipt_guarantee_passes():
    from boundary.selftest import check_receipt_verifies
    res = check_receipt_verifies()
    assert res.passed, res.detail
