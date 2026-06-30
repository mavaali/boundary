"""Unit tests for envelope write accounting.

Covers:
- Failed write_file calls (exception or 'ERROR:' return) do NOT consume max_writes
- Successful write_file calls do consume max_writes
- append_file is gated by max_appends, NOT max_writes
- append_file requires the file to exist (continuation-of-write semantics)
- Path refusal does not consume max_writes
- max_writes refusal returns without executing the underlying fn
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boundary.envelope import Envelope, EnvelopeEvent, _make_enforced_tool, _stage_proposal_tool
from boundary.tools.fs import register_fs_tools
from boundary.tools.registry import ToolRegistry
from boundary.tools.workspace import Workspace


def _harness(tmp_path: Path, envelope: Envelope):
    """Build an enforced registry against a real Workspace + fs tools."""
    envelope.require_staging = False
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_fs_tools(base, ws)
    counters: dict[str, int] = {}
    events: list[EnvelopeEvent] = []
    iter_ref = [1]
    enforced = ToolRegistry()
    for tool in base._tools.values():
        enforced.register(_make_enforced_tool(tool, envelope, counters, events, iter_ref))
    return enforced, counters, events, ws


def _staging_harness(tmp_path: Path, envelope: Envelope):
    """Build an enforced registry with the staging pivot enabled."""
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_fs_tools(base, ws)
    counters: dict[str, int] = {}
    events: list[EnvelopeEvent] = []
    iter_ref = [1]
    enforced = ToolRegistry()
    for tool in base._tools.values():
        enforced.register(_make_enforced_tool(tool, envelope, counters, events, iter_ref))
    enforced.register(_stage_proposal_tool(counters, events, iter_ref))
    return enforced, counters, events, ws


def test_successful_write_increments_executed(tmp_path):
    env = Envelope(writable_paths=["out.md"], max_writes=2)
    enforced, counters, events, ws = _harness(tmp_path, env)
    r = enforced.get("write_file").call({"path": "out.md", "content": "hi", "reason": "test"})
    assert r.startswith("wrote ")
    assert counters["writes_attempted"] == 1
    assert counters["writes_executed"] == 1


def test_typeerror_does_not_consume_budget(tmp_path):
    env = Envelope(writable_paths=["out.md"], max_writes=2)
    enforced, counters, events, ws = _harness(tmp_path, env)
    # Omit required 'content' kwarg → TypeError inside original_fn
    with pytest.raises(TypeError):
        enforced.get("write_file").call({"path": "out.md", "reason": "test"})
    assert counters["writes_attempted"] == 1
    assert counters.get("writes_executed", 0) == 0
    assert any(e.kind == "write_failed" for e in events)


def test_error_sentinel_does_not_consume_budget(tmp_path):
    env = Envelope(writable_paths=["does-not-exist.md"], max_writes=2)
    enforced, counters, events, ws = _harness(tmp_path, env)
    # edit_file on a nonexistent file → returns "ERROR: file not found"
    r = enforced.get("edit_file").call({
        "path": "does-not-exist.md", "old_str": "a", "new_str": "b", "reason": "test",
    })
    assert r.startswith("ERROR:")
    assert counters["writes_attempted"] == 1
    assert counters.get("writes_executed", 0) == 0


def test_path_refusal_does_not_consume_budget(tmp_path):
    env = Envelope(writable_paths=["allowed.md"], max_writes=2)
    enforced, counters, events, ws = _harness(tmp_path, env)
    r = enforced.get("write_file").call({"path": "elsewhere.md", "content": "x", "reason": "t"})
    assert "ENVELOPE REFUSED" in r
    assert counters["writes_attempted"] == 1
    assert counters.get("writes_executed", 0) == 0


def test_max_writes_refusal_does_not_execute(tmp_path):
    env = Envelope(writable_paths=["out.md"], max_writes=1)
    enforced, counters, events, ws = _harness(tmp_path, env)
    enforced.get("write_file").call({"path": "out.md", "content": "a", "reason": "t"})
    r = enforced.get("write_file").call({"path": "out.md", "content": "b", "reason": "t"})
    assert "ENVELOPE REFUSED: max_writes" in r
    # File should still contain only the first write
    assert (tmp_path / "out.md").read_text() == "a"
    assert counters["writes_executed"] == 1


def test_append_file_does_not_consume_writes(tmp_path):
    env = Envelope(writable_paths=["out.md"], max_writes=1, max_appends=5)
    enforced, counters, events, ws = _harness(tmp_path, env)
    enforced.get("write_file").call({"path": "out.md", "content": "chunk1", "reason": "t"})
    for i in range(3):
        r = enforced.get("append_file").call({"path": "out.md", "content": f"-{i}", "reason": "t"})
        assert r.startswith("appended ")
    assert counters["writes_executed"] == 1
    assert counters["appends_executed"] == 3
    assert (tmp_path / "out.md").read_text() == "chunk1-0-1-2"


def test_append_file_requires_existing_file(tmp_path):
    env = Envelope(writable_paths=["out.md"], max_writes=1, max_appends=5)
    enforced, counters, events, ws = _harness(tmp_path, env)
    r = enforced.get("append_file").call({"path": "out.md", "content": "x", "reason": "t"})
    assert r.startswith("ERROR: file not found")
    assert counters.get("appends_executed", 0) == 0


def test_append_file_respects_max_appends(tmp_path):
    env = Envelope(writable_paths=["out.md"], max_writes=1, max_appends=2)
    enforced, counters, events, ws = _harness(tmp_path, env)
    enforced.get("write_file").call({"path": "out.md", "content": "x", "reason": "t"})
    enforced.get("append_file").call({"path": "out.md", "content": "a", "reason": "t"})
    enforced.get("append_file").call({"path": "out.md", "content": "b", "reason": "t"})
    r = enforced.get("append_file").call({"path": "out.md", "content": "c", "reason": "t"})
    assert "ENVELOPE REFUSED: max_appends" in r
    assert (tmp_path / "out.md").read_text() == "xab"


def test_append_file_path_refusal(tmp_path):
    env = Envelope(writable_paths=["allowed.md"], max_writes=1, max_appends=5)
    enforced, counters, events, ws = _harness(tmp_path, env)
    r = enforced.get("append_file").call({"path": "elsewhere.md", "content": "x", "reason": "t"})
    assert "ENVELOPE REFUSED" in r
    assert counters.get("appends_executed", 0) == 0


def test_missing_reason_blocks_write(tmp_path):
    env = Envelope(writable_paths=["out.md"], max_writes=1, require_reason=True)
    enforced, counters, events, ws = _harness(tmp_path, env)
    r = enforced.get("write_file").call({"path": "out.md", "content": "x"})
    assert "ENVELOPE REFUSED" in r and "reason" in r
    # Missing reason short-circuits before write accounting
    assert counters.get("writes_attempted", 0) == 0
    assert counters.get("writes_executed", 0) == 0


def test_unstaged_orientation_reads_then_refusal(tmp_path):
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.md").write_text("b")
    (tmp_path / "c.md").write_text("c")
    env = Envelope(writable_paths=["out.md"], max_unstaged_reads=2)
    enforced, counters, events, ws = _staging_harness(tmp_path, env)

    assert enforced.get("read_file").call({"path": "a.md"}) == "a"
    assert enforced.get("read_file").call({"path": "b.md"}) == "b"
    r = enforced.get("read_file").call({"path": "c.md"})

    assert "stage_proposal" in r
    assert counters["unstaged_reads"] == 3
    assert any(e.kind == "staging_required" and e.tool == "read_file" for e in events)


def test_stage_proposal_reopens_deep_reads(tmp_path):
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.md").write_text("b")
    env = Envelope(writable_paths=["out.md"], max_unstaged_reads=1)
    enforced, counters, events, ws = _staging_harness(tmp_path, env)

    assert enforced.get("read_file").call({"path": "a.md"}) == "a"
    refused = enforced.get("read_file").call({"path": "b.md"})
    assert "stage_proposal" in refused

    staged = enforced.get("stage_proposal").call({
        "thesis": "The likely issue is unbounded reading.",
        "hypotheses": ["read phase lacks a pivot"],
        "evidence_plan": ["read one more discriminating file"],
        "intended_write": "out.md",
        "cost_class": "review",
        "kill_criteria": ["evidence contradicts thesis"],
    })
    assert staged.startswith("STAGED:")
    assert enforced.get("read_file").call({"path": "b.md"}) == "b"
    assert counters["staged"] == 1
    assert any(e.kind == "staged" for e in events)


def test_write_requires_staging(tmp_path):
    env = Envelope(writable_paths=["out.md"])
    enforced, counters, events, ws = _staging_harness(tmp_path, env)

    r = enforced.get("write_file").call({"path": "out.md", "content": "x", "reason": "test"})
    assert "stage_proposal" in r
    assert counters.get("writes_attempted", 0) == 0
    assert not (tmp_path / "out.md").exists()

    enforced.get("stage_proposal").call({
        "thesis": "Write the minimal output.",
        "hypotheses": ["output is needed"],
        "evidence_plan": ["no more reads"],
    })
    r = enforced.get("write_file").call({"path": "out.md", "content": "x", "reason": "test"})
    assert r.startswith("wrote ")
    assert (tmp_path / "out.md").read_text() == "x"
