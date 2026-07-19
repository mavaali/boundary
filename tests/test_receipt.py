"""Run receipts (`boundary.receipt/v1`).

A receipt binds the two artifacts that already exist independently — the policy
(`Envelope.spec_dict()`/`spec_hash()`) and the grade
(`ThirdUmpireReport` / `boundary.third-umpire/v1`) — into one portable claim:
*this run executed inside this exact envelope, and here is the grade*.

`verify` re-hashes the embedded spec (must match `spec_hash`) and re-grades the
transcript (verdict must match), so a stored receipt cannot silently drift from
the policy it names or the run it grades.
"""
from __future__ import annotations

import json
from pathlib import Path

from boundary.envelope import Envelope, canonical_spec_hash
from boundary.receipt import Receipt, verify_receipt
from boundary.third_umpire import ThirdUmpire


def _transcript(tmp_path: Path, env: Envelope, *, refuse_write: bool = False) -> Path:
    """A minimal but real transcript carrying the envelope spec in envelope_start,
    exactly as EnvelopeRunner logs it."""
    write_events = []
    if refuse_write:
        write_events.append({"kind": "write_refused", "tool": "write_file",
                             "detail": "path=escape.md", "iteration": 2})
    events = [
        {"type": "envelope_start", "writable_paths": env.writable_paths,
         "require_staging": env.require_staging,
         "spec": env.spec_dict(), "spec_hash": env.spec_hash(), "task": "t"},
        {"type": "envelope_end", "writes_executed": 0 if refuse_write else 1,
         "min_writes": env.min_writes, "on_commit": env.on_commit,
         "on_taint": env.on_taint, "model": "claude-haiku-4.5",
         "estimated_dollars": 0.01, "events": write_events},
        {"type": "end", "iterations": 2},
    ]
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return p


def test_receipt_has_schema_and_binds_spec_to_verdict(tmp_path):
    env = Envelope(writable_paths=["out.md"], max_writes=3)
    tp = _transcript(tmp_path, env)
    report = ThirdUmpire.grade(tp)
    r = Receipt.build(report, spec=env.spec_dict(), spec_hash=env.spec_hash(),
                      run_id=7, schedule_name="wiki", model="claude-haiku-4.5",
                      estimated_dollars=0.01, transcript_path=str(tp), created_at=123)
    d = r.as_dict()
    assert d["schema"] == "boundary.receipt/v1"
    assert d["spec_hash"] == env.spec_hash()
    assert d["spec"] == env.spec_dict()
    assert d["verdict"]["schema"] == "boundary.third-umpire/v1"
    assert d["verdict"]["verdict"] == report.verdict
    assert d["run_id"] == 7
    assert d["schedule_name"] == "wiki"


def test_receipt_json_roundtrips(tmp_path):
    env = Envelope(writable_paths=["out.md"])
    tp = _transcript(tmp_path, env)
    report = ThirdUmpire.grade(tp)
    r = Receipt.build(report, spec=env.spec_dict(), spec_hash=env.spec_hash(),
                      transcript_path=str(tp), created_at=1)
    back = Receipt.from_dict(json.loads(r.to_json()))
    assert back.spec_hash == r.spec_hash
    assert back.verdict == r.verdict
    assert back.as_dict() == r.as_dict()


def test_verify_passes_for_intact_receipt(tmp_path):
    env = Envelope(writable_paths=["out.md"])
    tp = _transcript(tmp_path, env)
    report = ThirdUmpire.grade(tp)
    r = Receipt.build(report, spec=env.spec_dict(), spec_hash=env.spec_hash(),
                      transcript_path=str(tp), created_at=1)
    res = verify_receipt(r)
    assert res.ok
    assert res.spec_hash_ok and res.verdict_ok


def test_verify_detects_tampered_spec(tmp_path):
    env = Envelope(writable_paths=["out.md"], max_writes=3)
    tp = _transcript(tmp_path, env)
    report = ThirdUmpire.grade(tp)
    r = Receipt.build(report, spec=env.spec_dict(), spec_hash=env.spec_hash(),
                      transcript_path=str(tp), created_at=1)
    # An attacker widens the allowlist in the stored receipt but leaves the hash.
    r.spec["writable_paths"] = ["**"]
    res = verify_receipt(r)
    assert not res.ok
    assert not res.spec_hash_ok


def test_verify_detects_tampered_verdict(tmp_path):
    env = Envelope(writable_paths=["out.md"])
    tp = _transcript(tmp_path, env)
    report = ThirdUmpire.grade(tp)
    r = Receipt.build(report, spec=env.spec_dict(), spec_hash=env.spec_hash(),
                      transcript_path=str(tp), created_at=1)
    # Claim PASS when the transcript re-grades to something else.
    r.verdict["verdict"] = "PASS" if report.verdict != "PASS" else "FAIL"
    res = verify_receipt(r)
    assert not res.ok
    assert not res.verdict_ok


def test_verify_detects_swapped_transcript(tmp_path):
    """Pointing the receipt at a transcript whose policy differs must fail even
    if that transcript grades to the same verdict letter."""
    env = Envelope(writable_paths=["out.md"], max_writes=3)
    tp = _transcript(tmp_path, env)
    report = ThirdUmpire.grade(tp)
    r = Receipt.build(report, spec=env.spec_dict(), spec_hash=env.spec_hash(),
                      transcript_path=str(tp), created_at=1)
    other = Envelope(writable_paths=["out.md"], max_writes=99)
    tp2 = tmp_path / "other.jsonl"
    tp2.write_text((_transcript(tmp_path, other)).read_text(), encoding="utf-8")
    r.transcript_path = str(tp2)
    res = verify_receipt(r)
    assert not res.ok


def test_verify_spec_only_when_transcript_missing(tmp_path):
    env = Envelope(writable_paths=["out.md"])
    tp = _transcript(tmp_path, env)
    report = ThirdUmpire.grade(tp)
    r = Receipt.build(report, spec=env.spec_dict(), spec_hash=env.spec_hash(),
                      transcript_path=str(tmp_path / "gone.jsonl"), created_at=1)
    res = verify_receipt(r)
    # spec integrity still checkable; verdict re-grade is skipped, not failed
    assert res.spec_hash_ok
    assert "transcript" in res.detail.lower()


def test_canonical_spec_hash_matches_envelope_method():
    env = Envelope(writable_paths=["out.md"], on_taint="refuse")
    assert canonical_spec_hash(env.spec_dict()) == env.spec_hash()
