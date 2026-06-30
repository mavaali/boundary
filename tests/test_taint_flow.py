"""Item 3 — taint/provenance dimension.

Coarse, run-level taint: once the agent reads untrusted external content
(fetch_url), any subsequent write to a writable path is a potential exfiltration
channel. The taint gate fires a `taint_flow` event at the write sink and applies
the `on_taint` policy (refuse / warn / allow). A run that only reads workspace
files and writes must NOT trigger it.
"""
from __future__ import annotations

from pathlib import Path

from boundary.envelope import Envelope, EnvelopeEvent, _make_enforced_tool
from boundary.tools.fs import register_fs_tools
from boundary.tools.registry import Tool, ToolRegistry
from boundary.tools.workspace import Workspace


def _fetch_tool() -> Tool:
    def fetch_url(url: str, reason: str = "") -> str:
        return f"<untrusted content from {url}>"

    return Tool(
        name="fetch_url",
        description="fetch a URL (external/untrusted)",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["url", "reason"],
        },
        fn=fetch_url,
        kind="external",
    )


def _harness(tmp_path: Path, envelope: Envelope):
    envelope.require_staging = False  # isolate the taint gate
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_fs_tools(base, ws)
    base.register(_fetch_tool())
    counters: dict[str, int] = {}
    events: list[EnvelopeEvent] = []
    iter_ref = [1]
    enforced = ToolRegistry()
    for tool in base._tools.values():
        enforced.register(_make_enforced_tool(tool, envelope, counters, events, iter_ref))
    return enforced, counters, events, ws


def test_tainted_fetch_then_write_warns_but_proceeds(tmp_path):
    env = Envelope(writable_paths=["out.md"], on_taint="warn")
    enforced, counters, events, ws = _harness(tmp_path, env)
    enforced.get("fetch_url").call({"url": "http://evil.test", "reason": "r"})
    r = enforced.get("write_file").call({"path": "out.md", "content": "hi", "reason": "r"})
    assert r.startswith("wrote "), r
    assert (tmp_path / "out.md").exists()
    assert any(e.kind == "taint_flow" for e in events)


def test_tainted_fetch_then_write_refused_when_on_taint_refuse(tmp_path):
    env = Envelope(writable_paths=["out.md"], on_taint="refuse")
    enforced, counters, events, ws = _harness(tmp_path, env)
    enforced.get("fetch_url").call({"url": "http://evil.test", "reason": "r"})
    r = enforced.get("write_file").call({"path": "out.md", "content": "hi", "reason": "r"})
    assert "ENVELOPE REFUSED" in r and "taint" in r.lower(), r
    assert not (tmp_path / "out.md").exists()
    assert any(e.kind == "taint_flow" for e in events)


def test_workspace_only_write_does_not_taint(tmp_path):
    env = Envelope(writable_paths=["out.md"], on_taint="warn")
    enforced, counters, events, ws = _harness(tmp_path, env)
    # read a workspace file, then write — no external/untrusted source touched
    (tmp_path / "src.md").write_text("local", encoding="utf-8")
    enforced.get("read_file").call({"path": "src.md"})
    r = enforced.get("write_file").call({"path": "out.md", "content": "hi", "reason": "r"})
    assert r.startswith("wrote "), r
    assert not any(e.kind == "taint_flow" for e in events)


def test_third_umpire_surfaces_taint_flow(tmp_path):
    import json

    from boundary.third_umpire import ThirdUmpire

    events = [
        {"type": "envelope_start", "require_staging": True, "writable_paths": ["out.md"]},
        {"type": "envelope_end", "on_commit": "refuse", "on_taint": "warn", "tainted_reads": 1,
         "events": [{"kind": "taint_flow", "tool": "write_file",
                     "detail": "on_taint=warn sources=['http://evil.test']", "iteration": 2}]},
        {"type": "end", "iterations": 2},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    report = ThirdUmpire.grade(p)
    tf = [c for c in report.checks if c.name == "taint_flow"]
    assert len(tf) == 1
    assert not tf[0].passed and tf[0].severity == "warn"
    assert "1" in tf[0].detail


def test_third_umpire_no_taint_flow_on_clean_run(tmp_path):
    import json

    from boundary.third_umpire import ThirdUmpire

    events = [
        {"type": "envelope_start", "require_staging": True, "writable_paths": ["out.md"]},
        {"type": "envelope_end", "on_commit": "refuse", "on_taint": "warn", "tainted_reads": 0, "events": []},
        {"type": "end", "iterations": 1},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    report = ThirdUmpire.grade(p)
    assert [c for c in report.checks if c.name == "taint_flow"] == []


def test_on_taint_allow_is_silent(tmp_path):
    env = Envelope(writable_paths=["out.md"], on_taint="allow")
    enforced, counters, events, ws = _harness(tmp_path, env)
    enforced.get("fetch_url").call({"url": "http://evil.test", "reason": "r"})
    r = enforced.get("write_file").call({"path": "out.md", "content": "hi", "reason": "r"})
    assert r.startswith("wrote "), r
    assert not any(e.kind == "taint_flow" for e in events)
