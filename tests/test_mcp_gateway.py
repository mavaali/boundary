"""Unit tests for the MCP gateway (boundary/mcp_gateway.py).

The Gateway core is exercised directly — no `mcp` package needed. Covers:
- served tool list: prefixed names, engine schemas passed through, status tool
- write allowlist refusal vs allowed write (and the write actually landing)
- require_reason on write tools
- staging pivot: orientation reads exhaust, stage_proposal re-opens reads
- max_writes ceiling
- taint: reading a ledger-tainted file gates a later write (warn and refuse)
- pre-exec validation: malformed calls rejected without executing
- unknown tool error, status counters, transcript events
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boundary.envelope import Envelope
from boundary.mcp_gateway import STATUS_TOOL, Gateway
from boundary.taint import TaintStore
from boundary.transcript import Transcript


@pytest.fixture(autouse=True)
def _isolated_taint_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "boundary-home"))


def _gateway(tmp_path: Path, **env_overrides) -> Gateway:
    env = Envelope(
        writable_paths=["out/**"],
        max_writes=2,
        require_staging=False,
        max_input_tokens=None,
        max_output_tokens=None,
        max_wall_seconds=None,
    )
    for k, v in env_overrides.items():
        setattr(env, k, v)
    return Gateway(tmp_path / "ws", env, transcript=False)


def test_list_tools_prefixed_with_schemas(tmp_path):
    gw = _gateway(tmp_path, require_staging=True)
    tools = {t["name"]: t for t in gw.list_tools()}
    assert "boundary_read_file" in tools
    assert "boundary_write_file" in tools
    assert "boundary_stage_proposal" in tools
    assert STATUS_TOOL in tools
    # bash is not served unless enable_shell was requested
    assert "boundary_bash" not in tools
    # engine schemas pass through untouched, including the reason contract
    assert "reason" in tools["boundary_write_file"]["inputSchema"]["required"]


def test_write_allowlist_enforced_and_write_lands(tmp_path):
    gw = _gateway(tmp_path)
    refused = gw.call("boundary_write_file", {
        "path": "secrets/x.txt", "content": "no", "reason": "test"})
    assert refused.startswith("ENVELOPE REFUSED")
    assert not (tmp_path / "ws" / "secrets" / "x.txt").exists()

    ok = gw.call("boundary_write_file", {
        "path": "out/a.txt", "content": "hello", "reason": "test"})
    assert not ok.startswith("ENVELOPE REFUSED")
    assert (tmp_path / "ws" / "out" / "a.txt").read_text() == "hello"


def test_write_requires_reason(tmp_path):
    gw = _gateway(tmp_path)
    r = gw.call("boundary_write_file", {"path": "out/a.txt", "content": "x", "reason": ""})
    assert r.startswith("ENVELOPE REFUSED")
    assert "reason" in r


def test_staging_pivot_over_mcp(tmp_path):
    gw = _gateway(tmp_path, require_staging=True, max_unstaged_reads=1)
    (tmp_path / "ws" / "f.txt").write_text("data")
    assert gw.call("boundary_read_file", {"path": "f.txt"}) == "data"
    blocked = gw.call("boundary_read_file", {"path": "f.txt"})
    assert blocked.startswith("ENVELOPE REFUSED")
    # a write before staging is refused too
    w = gw.call("boundary_write_file", {"path": "out/a.txt", "content": "x", "reason": "r"})
    assert w.startswith("ENVELOPE REFUSED")
    staged = gw.call("boundary_stage_proposal", {
        "thesis": "t", "hypotheses": ["h"], "evidence_plan": ["e"]})
    assert staged.startswith("STAGED")
    assert gw.call("boundary_read_file", {"path": "f.txt"}) == "data"
    ok = gw.call("boundary_write_file", {"path": "out/a.txt", "content": "x", "reason": "r"})
    assert not ok.startswith("ENVELOPE REFUSED")


def test_max_writes_ceiling(tmp_path):
    gw = _gateway(tmp_path, max_writes=1)
    gw.call("boundary_write_file", {"path": "out/a.txt", "content": "1", "reason": "r"})
    over = gw.call("boundary_write_file", {"path": "out/b.txt", "content": "2", "reason": "r"})
    assert over.startswith("ENVELOPE REFUSED")
    assert "max_writes" in over


def test_taint_read_gates_write(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "vendor.md").write_text("untrusted")
    store = TaintStore.load(ws)
    store.mark_file("vendor.md")

    # warn: write proceeds but the flow is recorded and the output is marked
    gw = _gateway(tmp_path, on_taint="warn")
    gw.call("boundary_read_file", {"path": "vendor.md"})
    ok = gw.call("boundary_write_file", {"path": "out/o.txt", "content": "x", "reason": "r"})
    assert not ok.startswith("ENVELOPE REFUSED")
    assert any(e.kind == "taint_flow" for e in gw._events)
    assert TaintStore.load(ws).is_tainted("out/o.txt")

    # refuse: the write is blocked
    gw2 = _gateway(tmp_path, on_taint="refuse")
    gw2.call("boundary_read_file", {"path": "vendor.md"})
    blocked = gw2.call("boundary_write_file", {"path": "out/p.txt", "content": "x", "reason": "r"})
    assert blocked.startswith("ENVELOPE REFUSED")
    assert not (ws / "out" / "p.txt").exists()


def test_prevalidation_rejects_malformed_call(tmp_path):
    gw = _gateway(tmp_path)
    r = gw.call("boundary_write_file", {"reason": "r"})
    assert r.startswith("ERROR: invalid call")
    assert gw._counters.get("writes_attempted", 0) == 0


def test_unknown_tool(tmp_path):
    gw = _gateway(tmp_path)
    r = gw.call("boundary_nope", {})
    assert r.startswith("ERROR: unknown tool")
    assert "boundary_read_file" in r


def test_status_reports_counters_and_spec(tmp_path):
    gw = _gateway(tmp_path)
    gw.call("boundary_write_file", {"path": "out/a.txt", "content": "x", "reason": "r"})
    status = json.loads(gw.call(STATUS_TOOL, None))
    assert status["counters"]["writes_executed"] == 1
    assert status["counters"]["calls"] == 1  # status itself is not counted
    assert status["spec"]["writable_paths"] == ["out/**"]
    assert status["spec_hash"] == gw.envelope.spec_hash()
    assert status["min_writes_met"] is True


def test_transcript_events(tmp_path):
    env = Envelope(writable_paths=["out/**"], require_staging=False,
                   max_input_tokens=None, max_output_tokens=None, max_wall_seconds=None)
    tpath = tmp_path / "t.jsonl"
    gw = Gateway(tmp_path / "ws", env, transcript=Transcript(path=tpath))
    gw.call("boundary_write_file", {"path": "out/a.txt", "content": "x", "reason": "r"})
    gw.call("boundary_write_file", {"path": "nope/b.txt", "content": "x", "reason": "r"})
    gw.close()
    events = [json.loads(line) for line in tpath.read_text().splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "envelope_start"
    assert kinds[-1] == "envelope_end"
    assert events[0]["spec_hash"] == env.spec_hash()
    results = [e for e in events if e["type"] == "tool_result"]
    assert [r["result_class"] for r in results] == ["success", "policy-refused"]
    assert events[-1]["writes_executed"] == 1
