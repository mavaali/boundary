"""Integration test: credential_scopes declared on the Envelope must reach the
sandbox that actually runs bash.

Guards against the silent no-op where scopes are validated, logged into the
transcript, and graded 'held' — yet never handed to the credential proxy, so
the agent wields an unbounded credential. The bash tool reads scopes from
`agent.credential_scopes` (live), which `EnvelopeRunner.run()` must bind from
the envelope's policy. This is the seam the component-level e2e probe skipped.

nono-free: the precondition's `shutil.which` and the sandbox call are stubbed,
so this runs everywhere and isolates the wiring seam.
"""
from __future__ import annotations

import boundary.envelope as envelope_mod
import boundary.tools.shell as shell_mod
from boundary.agent import Agent
from boundary.clients.base import ChatResponse, Message, ToolCall
from boundary.credential_proxy import CredentialScope
from boundary.envelope import Envelope, EnvelopeRunner


class _OneBashThenStop:
    """Emits a single bash tool call, then stops."""
    model = "claude-sonnet-4.6"

    def __init__(self):
        self.i = 0

    def chat(self, messages, tools=None, **kw):
        self.i += 1
        if self.i == 1:
            tc = ToolCall(id="c1", name="bash",
                          arguments={"command": "env", "reason": "probe"})
            return ChatResponse(
                message=Message(role="assistant", content="", tool_calls=[tc]),
                finish_reason="tool_calls",
                input_tokens=1, output_tokens=1, cached_input_tokens=0,
            )
        return ChatResponse(
            message=Message(role="assistant", content="done"),
            finish_reason="stop",
            input_tokens=1, output_tokens=1, cached_input_tokens=0,
        )


def _scope():
    return CredentialScope(
        service="github", host="api.github.com",
        credential_key="env://GITHUB_TOKEN",
        allow_endpoints=["GET:/repos/*/pulls"],
    )


def _run(tmp_path, monkeypatch, captured):
    # Pretend nono is installed so the fail-closed precondition passes.
    monkeypatch.setattr(
        envelope_mod.shutil, "which",
        lambda name: "/usr/bin/nono" if name == "nono" else None,
    )

    def _fake_run_sandboxed(command, *, workspace_root, timeout, driver,
                            egress_allowlist=None, deny_read=None,
                            credential_scopes=None):
        captured["driver"] = driver
        captured["credential_scopes"] = credential_scopes
        return "ok"

    monkeypatch.setattr(shell_mod, "run_sandboxed", _fake_run_sandboxed)

    agent = Agent(name="d", system_prompt="x", workspace=str(tmp_path),
                  client=_OneBashThenStop(), enable_fs=False, enable_shell=True,
                  enable_web=False, transcript=False, sandbox_driver="nono")
    env = Envelope(writable_paths=["out.md"], require_staging=False,
                   credential_scopes=[_scope()])
    return EnvelopeRunner(agent, env).run("go"), agent


def test_declared_scopes_reach_the_sandbox(tmp_path, monkeypatch):
    captured: dict = {}
    _run(tmp_path, monkeypatch, captured)

    # The bash command ran under the nono driver...
    assert captured.get("driver") == "nono"
    # ...and the declared scopes actually reached it. The no-op bug left this
    # None/[]: scopes were on the envelope but never bound onto the agent.
    assert captured.get("credential_scopes"), (
        "credential_scopes declared on the Envelope never reached the sandbox — "
        "silent no-op (the runner didn't bind them onto the agent that runs bash)"
    )
    assert captured["credential_scopes"][0].service == "github"


def test_runner_binds_scopes_onto_the_agent(tmp_path, monkeypatch):
    # The agent that executes bash must carry the envelope's scopes after the run.
    _, agent = _run(tmp_path, monkeypatch, {})
    assert agent.credential_scopes and agent.credential_scopes[0].service == "github"
