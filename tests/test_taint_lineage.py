"""Item 1 (v2) — cross-run taint lineage (memory-poisoning defense).

Scheduled runs write files that later runs read. A tainted run's outputs are a
persistence channel for injection, so reading a file written by a tainted,
un-reviewed run inherits that taint (event `taint_inherited`), which then arms
the existing same-run taint gate at the write sink. Human review is the
declassifier: `boundary review approve <run-id>` clears a run's lineage.
"""
from __future__ import annotations

import json

from boundary.envelope import Envelope
from boundary.history import History
from boundary.kernel import PolicyKernel


def _record(history: History, *, tainted: bool, written: list[str],
            schedule: str = "wiki-health") -> int:
    return history.record_run(
        schedule_name=schedule, persona="fury", workspace="/ws",
        started_at=1000.0, ended_at=1001.0, stop_reason="stop", iterations=3,
        writes_executed=len(written), input_tokens=10, output_tokens=5,
        cached_input_tokens=0, estimated_dollars=0.0, wall_seconds=1.0,
        third_umpire_verdict="PASS", third_umpire_summary={},
        transcript_path=None, written_files=written,
        tainted=tainted, taint_sources=["http://evil.test"] if tainted else [],
    )


def test_history_records_taint_columns(tmp_path):
    h = History(tmp_path / "h.db")
    run_id = _record(h, tainted=True, written=["/ws/notes.md"])
    row = h.list_runs(limit=1)[0]
    assert row["id"] == run_id
    assert row["tainted"] == 1
    assert json.loads(row["taint_sources_json"]) == ["http://evil.test"]
    assert row["taint_cleared"] == 0
    h.close()


def test_taint_provenance_flags_file_written_by_tainted_run(tmp_path):
    h = History(tmp_path / "h.db")
    run_id = _record(h, tainted=True, written=["/ws/notes.md"])
    prov = h.taint_provenance("/ws/notes.md")
    assert prov is not None
    assert prov["run_id"] == run_id
    assert prov["sources"] == ["http://evil.test"]
    assert h.taint_provenance("/ws/other.md") is None
    h.close()


def test_taint_provenance_clean_run_is_none(tmp_path):
    h = History(tmp_path / "h.db")
    _record(h, tainted=False, written=["/ws/notes.md"])
    assert h.taint_provenance("/ws/notes.md") is None
    h.close()


def test_taint_provenance_latest_writer_wins(tmp_path):
    h = History(tmp_path / "h.db")
    _record(h, tainted=True, written=["/ws/notes.md"])
    _record(h, tainted=False, written=["/ws/notes.md"])
    # The most recent writer of the file is clean, so its content governs.
    assert h.taint_provenance("/ws/notes.md") is None
    h.close()


def test_clear_taint_declassifies(tmp_path):
    h = History(tmp_path / "h.db")
    run_id = _record(h, tainted=True, written=["/ws/notes.md"])
    assert h.taint_provenance("/ws/notes.md") is not None
    h.clear_taint(run_id)
    assert h.taint_provenance("/ws/notes.md") is None
    h.close()


def test_make_provenance_resolves_workspace_relative_paths(tmp_path):
    h = History(tmp_path / "h.db")
    ws = tmp_path / "ws"
    run_id = _record(h, tainted=True, written=[str(ws / "notes.md")])
    resolve = h.make_provenance(ws)
    prov = resolve("notes.md")  # the path exactly as read_file receives it
    assert prov is not None and prov["run_id"] == run_id
    assert resolve("clean.md") is None
    h.close()


def test_kernel_read_inherits_taint_and_arms_write_gate():
    env = Envelope(writable_paths=["out.md"], on_taint="refuse")
    env.require_staging = False

    def provenance(path):
        if path == "notes.md":
            return {"run_id": 42, "schedule_name": "wiki-health",
                    "sources": ["http://evil.test"]}
        return None

    k = PolicyKernel(env, provenance=provenance)
    d = k.pre_tool("read_file", "read", {"path": "notes.md"})
    assert d.action == "allow"  # the read itself proceeds; it is labeled, not blocked
    assert k.counters.get("tainted_reads", 0) == 1
    inherited = [e for e in k.events if e.kind == "taint_inherited"]
    assert len(inherited) == 1
    assert "run=42" in inherited[0].detail
    # the same-run taint gate now refuses the write
    d2 = k.pre_tool("write_file", "write", {"path": "out.md", "content": "x", "reason": "r"})
    assert d2.action == "refuse"
    assert any(e.kind == "taint_flow" for e in k.events)


def test_kernel_read_without_provenance_stays_clean():
    env = Envelope(writable_paths=["out.md"], on_taint="refuse")
    env.require_staging = False
    k = PolicyKernel(env, provenance=lambda path: None)
    d = k.pre_tool("read_file", "read", {"path": "notes.md"})
    assert d.action == "allow"
    assert k.counters.get("tainted_reads", 0) == 0
    d2 = k.pre_tool("write_file", "write", {"path": "out.md", "content": "x", "reason": "r"})
    assert d2.action == "allow"
    assert not any(e.kind in ("taint_inherited", "taint_flow") for e in k.events)


def test_third_umpire_surfaces_taint_lineage(tmp_path):
    from boundary.third_umpire import ThirdUmpire

    events = [
        {"type": "envelope_start", "require_staging": True, "writable_paths": ["out.md"]},
        {"type": "envelope_end", "on_commit": "refuse", "on_taint": "warn", "tainted_reads": 1,
         "events": [{"kind": "taint_inherited", "tool": "read_file",
                     "detail": "path=notes.md run=42 sources=['http://evil.test']",
                     "iteration": 2}]},
        {"type": "end", "iterations": 2},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    report = ThirdUmpire.grade(p)
    tl = [c for c in report.checks if c.name == "taint_lineage"]
    assert len(tl) == 1
    assert not tl[0].passed and tl[0].severity == "warn"
    assert "run=42" in tl[0].detail


def test_third_umpire_no_lineage_line_on_clean_run(tmp_path):
    from boundary.third_umpire import ThirdUmpire

    events = [
        {"type": "envelope_start", "require_staging": True, "writable_paths": ["out.md"]},
        {"type": "envelope_end", "on_commit": "refuse", "on_taint": "warn",
         "tainted_reads": 0, "events": []},
        {"type": "end", "iterations": 1},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    report = ThirdUmpire.grade(p)
    assert [c for c in report.checks if c.name == "taint_lineage"] == []


def test_enforced_tool_passes_provenance_through(tmp_path):
    """_make_enforced_tool wires the provenance oracle into the kernel, so a
    runner-built registry inherits cross-run taint on read_file."""
    from boundary.envelope import _make_enforced_tool
    from boundary.kernel import EnvelopeEvent
    from boundary.tools.fs import register_fs_tools
    from boundary.tools.registry import ToolRegistry
    from boundary.tools.workspace import Workspace

    (tmp_path / "notes.md").write_text("poisoned", encoding="utf-8")
    env = Envelope(writable_paths=["out.md"], on_taint="refuse")
    env.require_staging = False
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_fs_tools(base, ws)
    counters: dict[str, int] = {}
    events: list[EnvelopeEvent] = []

    def provenance(path):
        if path == "notes.md":
            return {"run_id": 7, "schedule_name": "s", "sources": ["http://evil.test"]}
        return None

    enforced = ToolRegistry()
    for tool in base._tools.values():
        enforced.register(_make_enforced_tool(
            tool, env, counters, events, [1], provenance=provenance))

    assert enforced.get("read_file").call({"path": "notes.md"}) == "poisoned"
    r = enforced.get("write_file").call({"path": "out.md", "content": "x", "reason": "r"})
    assert "ENVELOPE REFUSED" in r and "taint" in r.lower()
    assert not (tmp_path / "out.md").exists()
    assert any(e.kind == "taint_inherited" for e in events)


def test_scout_hook_event_carries_taint(tmp_path, monkeypatch):
    from boundary import headless
    from boundary.schedule import ScheduleConfig

    event_dir = tmp_path / "events" / "pending"
    monkeypatch.setattr(headless, "EVENT_PENDING_DIR", event_dir)

    path = headless._emit_scout_hook_event(
        ScheduleConfig(
            name="wiki-health", schedule="daily 09:00", persona="fury",
            workspace=str(tmp_path), task="audit", writable_paths=["out.md"],
            notify={"scout_hook": {"on": "always"}},
        ),
        run_id=9, review_id=None, stop_reason="stop",
        third_umpire_verdict="WARN", transcript_path=None,
        written_files=[], error_text=None, rendered_paths=[],
        wall_seconds=1.0, estimated_dollars=0.0,
        tainted=True, taint_sources=["run:42:notes.md"],
    )
    assert path is not None
    event = json.loads((event_dir / "wiki-health-9.json").read_text())
    assert event["taint"] == {"tainted": True, "sources": ["run:42:notes.md"]}


def test_cli_review_approve_clears_taint(tmp_path):
    import subprocess
    import sys

    db = tmp_path / "h.db"
    h = History(db)
    run_id = _record(h, tainted=True, written=["/ws/notes.md"])
    h.close()

    proc = subprocess.run(
        [sys.executable, "-m", "boundary.cli", "review", "approve",
         str(run_id), "--db", str(db)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    h2 = History(db)
    assert h2.taint_provenance("/ws/notes.md") is None
    h2.close()
