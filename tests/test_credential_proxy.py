import shutil

import pytest

from boundary.credential_proxy import CredentialScope, compile_nono_flags


class TestCredentialScope:
    def test_constructs_with_all_fields(self):
        scope = CredentialScope(
            service="github",
            host="api.github.com",
            credential_key="env://GITHUB_TOKEN",
            allow_endpoints=["GET:/repos/*/pulls", "GET:/repos/*/issues"],
        )
        assert scope.service == "github"
        assert scope.host == "api.github.com"
        assert scope.credential_key == "env://GITHUB_TOKEN"
        assert scope.allow_endpoints == ["GET:/repos/*/pulls", "GET:/repos/*/issues"]

    def test_empty_allow_endpoints_rejected(self):
        with pytest.raises(ValueError, match="allow_endpoints"):
            CredentialScope(
                service="github", host="api.github.com",
                credential_key="env://GITHUB_TOKEN", allow_endpoints=[],
            )

    def test_empty_host_rejected(self):
        with pytest.raises(ValueError, match="host"):
            CredentialScope(
                service="github", host="",
                credential_key="env://GITHUB_TOKEN", allow_endpoints=["GET:/repos/*/pulls"],
            )

    def test_malformed_endpoint_rejected(self):
        with pytest.raises(ValueError, match="METHOD:/path"):
            CredentialScope(
                service="github", host="api.github.com",
                credential_key="env://GITHUB_TOKEN", allow_endpoints=["GET/repos"],
            )

    def test_as_spec_dict_round_trips_fields(self):
        scope = CredentialScope(
            service="github", host="api.github.com",
            credential_key="env://GITHUB_TOKEN", allow_endpoints=["GET:/repos/*/pulls"],
        )
        assert scope.as_spec_dict() == {
            "service": "github",
            "host": "api.github.com",
            "credential_key": "env://GITHUB_TOKEN",
            "allow_endpoints": ["GET:/repos/*/pulls"],
        }

    def test_from_dict_parses(self):
        scope = CredentialScope.from_dict({
            "service": "github", "host": "api.github.com",
            "credential_key": "env://GITHUB_TOKEN", "allow_endpoints": ["GET:/repos/*/pulls"],
        })
        assert scope.service == "github"
        assert scope.host == "api.github.com"


class TestCompileNonoFlags:
    def test_single_scope_single_endpoint_emits_all_three_flag_families(self):
        scopes = [CredentialScope(
            service="github", host="api.github.com",
            credential_key="env://GITHUB_TOKEN", allow_endpoints=["GET:/repos/*/pulls"],
        )]
        assert compile_nono_flags(scopes) == [
            "--credential", "github",
            "--allow-endpoint", "github:GET:/repos/*/pulls",
            "--allow-domain", "https://api.github.com/repos/*/pulls",
        ]

    def test_same_path_different_methods_dedupes_allow_domain(self):
        scopes = [CredentialScope(
            service="github", host="api.github.com",
            credential_key="env://GITHUB_TOKEN",
            allow_endpoints=["GET:/repos/*/pulls", "POST:/repos/*/pulls"],
        )]
        assert compile_nono_flags(scopes) == [
            "--credential", "github",
            "--allow-endpoint", "github:GET:/repos/*/pulls",
            "--allow-endpoint", "github:POST:/repos/*/pulls",
            "--allow-domain", "https://api.github.com/repos/*/pulls",
        ]

    def test_empty_scopes_yields_empty_flags(self):
        assert compile_nono_flags([]) == []


class TestNonoCommand:
    def test_builds_fs_egress_and_credential_flags(self, tmp_path):
        from boundary.tools.sandbox import _nono_command

        scope = CredentialScope(
            service="github", host="api.github.com",
            credential_key="env://GITHUB_TOKEN", allow_endpoints=["GET:/repos/*/pulls"],
        )
        cmd = _nono_command("echo hi", tmp_path, ["extra.example.com"], [scope])
        assert cmd[:6] == ["nono", "run", "--allow", str(tmp_path), "--allow-cwd", "-s"]
        # bash is resolved to an absolute path (nono binary resolution is
        # PATH-sensitive); assert the shape without pinning the exact path.
        # bash is resolved to an absolute path (nono binary resolution is
        # PATH-sensitive); assert the shape without pinning the exact path, and
        # tolerate Windows' 'bash.EXE'.
        assert cmd[-4] == "--" and "bash" in cmd[-3].lower()
        assert cmd[-2:] == ["-lc", "echo hi"]
        # Assert exact flag+value adjacency (not loose membership): the value
        # must immediately follow its flag, which also avoids a bare-hostname
        # substring check that reads as incomplete URL sanitization.
        pairs = list(zip(cmd, cmd[1:]))
        assert ("--allow-domain", "extra.example.com") in pairs
        assert ("--credential", "github") in pairs
        assert ("--allow-domain", "https://api.github.com/repos/*/pulls") in pairs
        assert "--block-net" not in cmd

    def test_block_net_when_sealed(self, tmp_path):
        from boundary.tools.sandbox import _nono_command

        cmd = _nono_command("echo hi", tmp_path, [], [])
        assert "--block-net" in cmd


class TestLiveReadWiring:
    def test_bash_reads_egress_and_credential_scopes_at_call_time(self, monkeypatch, tmp_path):
        import boundary.tools.shell as shell_mod
        from boundary.tools.registry import ToolRegistry
        from boundary.tools.workspace import Workspace

        captured: dict = {}

        def fake_run_sandboxed(command, **kwargs):
            captured.update(kwargs)
            return "ok"

        monkeypatch.setattr(shell_mod, "run_sandboxed", fake_run_sandboxed)

        class FakeAgent:
            egress_allowlist: list = []
            credential_scopes: list = []

        agent = FakeAgent()
        reg = ToolRegistry()
        shell_mod.register_shell_tools(
            reg, Workspace(str(tmp_path)), driver="nono", agent=agent
        )
        # The runner sets these AFTER tool registration:
        scope = CredentialScope(
            service="github", host="api.github.com",
            credential_key="env://GITHUB_TOKEN", allow_endpoints=["GET:/repos/*/pulls"],
        )
        agent.credential_scopes = [scope]
        agent.egress_allowlist = ["api.github.com"]

        reg.get("bash").call({"command": "echo hi", "reason": "x"})
        assert captured["credential_scopes"] == [scope]
        assert captured["egress_allowlist"] == ["api.github.com"]


requires_stack = pytest.mark.skipif(
    shutil.which("nono") is None,
    reason="nono binary required for the nono-driver e2e",
)


@requires_stack
class TestNonoDriverRuns:
    def test_command_executes_under_nono(self, tmp_path):
        from boundary.tools.sandbox import run_sandboxed

        out = run_sandboxed(
            "echo NONO_DRIVER_OK",
            workspace_root=str(tmp_path),
            timeout=60,
            driver="nono",
            egress_allowlist=[],
        )
        assert "NONO_DRIVER_OK" in out
