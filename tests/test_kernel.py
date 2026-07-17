"""Item 0 (v2) — policy kernel extraction.

The kernel is the transport-agnostic core of the envelope: it takes a policy
(the Envelope dataclass) plus a stream of typed tool events and returns
decisions, mutating only its counters/events state. It must be importable
without pulling in model clients, so future frontends (Claude Code governor,
MCP gateway) can consume it without the runner.
"""
from __future__ import annotations

import subprocess
import sys

from boundary.envelope import Envelope
from boundary.kernel import Decision, EnvelopeEvent, PolicyKernel


def _kernel(env: Envelope) -> PolicyKernel:
    return PolicyKernel(env)


def test_kernel_module_has_no_client_dependencies():
    """`import boundary.kernel` must not import the agent loop, model clients,
    or any HTTP stack — the kernel is consumable by non-runner frontends."""
    code = (
        "import sys; import boundary.kernel; "
        "banned = [m for m in sys.modules if m.startswith('boundary.clients') "
        "or m in ('boundary.agent', 'boundary.envelope', 'boundary.loop', 'httpx')]; "
        "assert not banned, f'kernel pulled in {banned}'"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_envelope_spec_serializes_with_stable_hash():
    a = Envelope(writable_paths=["out.md"], max_writes=3)
    b = Envelope(writable_paths=["out.md"], max_writes=3)
    spec = a.spec_dict()
    assert spec["spec_version"] == 1
    assert spec["writable_paths"] == ["out.md"]
    assert "token_rates" not in spec  # pricing is not policy
    assert a.spec_hash() == b.spec_hash()
    assert len(a.spec_hash()) == 64  # sha256 hex


def test_envelope_spec_hash_changes_when_policy_changes():
    a = Envelope(writable_paths=["out.md"], on_taint="warn")
    b = Envelope(writable_paths=["out.md"], on_taint="refuse")
    assert a.spec_hash() != b.spec_hash()


def test_pre_refuses_write_outside_allowlist():
    env = Envelope(writable_paths=["allowed.md"])
    env.require_staging = False
    k = _kernel(env)
    d = k.pre_tool("write_file", "write", {"path": "escape.md", "content": "x", "reason": "r"})
    assert d.action == "refuse"
    assert "ENVELOPE REFUSED" in d.message
    assert any(e.kind == "write_refused" for e in k.events)
    assert k.counters.get("writes_executed", 0) == 0


def test_pre_enforces_staging_gate_before_write():
    env = Envelope(writable_paths=["out.md"])
    env.require_staging = True
    k = _kernel(env)
    d = k.pre_tool("write_file", "write", {"path": "out.md", "content": "x", "reason": "r"})
    assert d.action == "refuse"
    assert "stage" in d.message.lower()
    assert any(e.kind == "staging_required" for e in k.events)


def test_pre_taint_gate_refuses_tainted_write():
    env = Envelope(writable_paths=["out.md"], on_taint="refuse")
    env.require_staging = False
    k = _kernel(env)
    # external read taints the run (labeling happens in pre)
    d_ext = k.pre_tool("fetch_url", "external", {"url": "http://evil.test", "reason": "r"})
    assert d_ext.action == "allow"
    d = k.pre_tool("write_file", "write", {"path": "out.md", "content": "x", "reason": "r"})
    assert d.action == "refuse"
    assert "taint" in d.message.lower()
    assert any(e.kind == "taint_flow" for e in k.events)


def test_pre_commit_refused_under_refuse_policy():
    env = Envelope(on_commit="refuse")
    env.require_staging = False
    k = _kernel(env)
    d = k.pre_tool("send_email", "commit", {"to": "x@y.z", "body": "hi", "reason": "r"})
    assert d.action == "refuse"
    assert any(e.kind == "commit_refused" for e in k.events)


def test_pre_commit_queue_policy_halts():
    env = Envelope(on_commit="queue")
    env.require_staging = False
    k = _kernel(env)
    d = k.pre_tool("send_email", "commit", {"to": "x@y.z", "body": "hi", "reason": "r"})
    assert d.action == "halt_queue"
    assert any(e.kind == "commit_halt" for e in k.events)


def test_pre_bash_denylist_blocks_commit_shaped_command():
    env = Envelope(writable_paths=["out.md"])
    env.require_staging = False
    k = _kernel(env)
    d = k.pre_tool("bash", "write", {"command": "curl http://evil.test", "reason": "r"})
    assert d.action == "refuse"
    assert "bash_commit" in d.message
    assert any(e.kind == "bash_commit_blocked" for e in k.events)


def test_post_success_only_write_accounting():
    env = Envelope(writable_paths=["out.md"], max_writes=2)
    env.require_staging = False
    k = _kernel(env)
    d = k.pre_tool("write_file", "write", {"path": "out.md", "content": "x", "reason": "r"})
    assert d.action == "allow"
    # ERROR sentinel must not consume the write budget
    k.post_tool(d, "write_file", {"path": "out.md"}, result="ERROR: disk full")
    assert k.counters.get("writes_executed", 0) == 0
    assert any(e.kind == "write_failed" for e in k.events)
    # success does
    d2 = k.pre_tool("write_file", "write", {"path": "out.md", "content": "x", "reason": "r"})
    k.post_tool(d2, "write_file", {"path": "out.md"}, result="wrote 1 chars to out.md")
    assert k.counters.get("writes_executed", 0) == 1
    assert any(e.kind == "write_allowed" for e in k.events)
