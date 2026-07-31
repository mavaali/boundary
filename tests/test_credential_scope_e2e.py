"""Load-bearing security probe: credential scoping under the live nono driver.

The guarantee made real: under a live nono sandbox, an out-of-scope method+path
is refused (403), external hosts are sealed, and the REAL credential never
appears in the jailed caller's environment (phantom-only inside; real only
upstream of nono's proxy). Skips when nono is not installed.
"""
import shutil

import pytest

from boundary.credential_proxy import CredentialScope
from boundary.tools.sandbox import run_sandboxed

requires_nono = pytest.mark.skipif(
    shutil.which("nono") is None,
    reason="nono binary required for the credential-scope e2e probe",
)

SENTINEL = "boundary-e2e-real-secret-sentinel-2c7f"


def _scope():
    return CredentialScope(
        service="github",
        host="api.github.com",
        credential_key="env://GITHUB_TOKEN",
        allow_endpoints=["GET:/repos/*/pulls"],
    )


@requires_nono
class TestCredentialScopeProbe:
    def test_real_credential_absent_inside_jail(self, tmp_path, monkeypatch):
        """The phantom-token guarantee: the jailed caller can dump its entire
        environment and never see the real secret."""
        monkeypatch.setenv("GITHUB_TOKEN", SENTINEL)
        out = run_sandboxed(
            "env",
            workspace_root=str(tmp_path), timeout=60,
            driver="nono", egress_allowlist=[], credential_scopes=[_scope()],
        )
        # Positive guard: env actually ran under nono (else the negative
        # assertion below would pass vacuously on a nono execution error).
        assert "PATH=" in out, f"env did not run under nono: {out!r}"
        assert SENTINEL not in out, (
            "REAL CREDENTIAL LEAKED into the jailed caller's environment"
        )

    def test_out_of_scope_endpoint_refused_403(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "sk_dummy_not_the_real_secret")
        out = run_sandboxed(
            'curl -s -o /dev/null -w "%{http_code}" '
            "https://api.github.com/repos/foo/bar/issues",
            workspace_root=str(tmp_path), timeout=60,
            driver="nono", egress_allowlist=[], credential_scopes=[_scope()],
        )
        assert "403" in out, f"out-of-scope endpoint was not refused: {out!r}"

    def test_external_host_sealed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "sk_dummy_not_the_real_secret")
        out = run_sandboxed(
            'curl -s -o /dev/null -w "%{http_code}" --max-time 10 https://example.com/',
            workspace_root=str(tmp_path), timeout=60,
            driver="nono", egress_allowlist=[], credential_scopes=[_scope()],
        )
        assert "200" not in out, f"external host was not sealed: {out!r}"

    def test_runner_enforces_end_to_end(self, tmp_path, monkeypatch):
        """Full path proof: EnvelopeRunner binds the scopes -> agent -> bash tool
        -> real nono driver. Unlike the probes above (which call run_sandboxed
        directly), this drives the actual run loop, so it fails if the runner
        ever stops wiring scopes onto the agent. The real secret must stay
        phantom in the bash output the agent sees."""
        from boundary.agent import Agent
        from boundary.clients.base import ChatResponse, Message, ToolCall
        from boundary.envelope import Envelope, EnvelopeRunner

        monkeypatch.setenv("GITHUB_TOKEN", SENTINEL)
        captured: dict = {}

        class _Client:
            model = "m"

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
                for m in messages:
                    if getattr(m, "role", None) == "tool" and m.content:
                        captured["out"] = m.content
                return ChatResponse(
                    message=Message(role="assistant", content="done"),
                    finish_reason="stop",
                    input_tokens=1, output_tokens=1, cached_input_tokens=0,
                )

        agent = Agent(name="d", system_prompt="x", workspace=str(tmp_path),
                      client=_Client(), enable_fs=False, enable_shell=True,
                      enable_web=False, transcript=False, sandbox_driver="nono")
        env = Envelope(writable_paths=["out.md"], require_staging=False,
                       credential_scopes=[_scope()])
        EnvelopeRunner(agent, env).run("go")

        assert "out" in captured, "bash tool result never reached the loop"
        # the bash actually executed under the jail (env printed something)...
        assert "PATH=" in captured["out"], f"env did not run under nono: {captured['out']!r}"
        # ...and the real secret stayed phantom through the full runner path.
        assert SENTINEL not in captured["out"], (
            "REAL CREDENTIAL LEAKED through the EnvelopeRunner path"
        )
