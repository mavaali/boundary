import shutil

import pytest

from boundary.credential_proxy import (
    CredentialScope,
    ProxyHandle,
    compile_nono_flags,
    parse_connection_info,
    start_credential_proxy,
)


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
                service="github",
                host="api.github.com",
                credential_key="env://GITHUB_TOKEN",
                allow_endpoints=[],
            )

    def test_empty_host_rejected(self):
        with pytest.raises(ValueError, match="host"):
            CredentialScope(
                service="github",
                host="",
                credential_key="env://GITHUB_TOKEN",
                allow_endpoints=["GET:/repos/*/pulls"],
            )

    def test_as_spec_dict_round_trips_fields(self):
        scope = CredentialScope(
            service="github",
            host="api.github.com",
            credential_key="env://GITHUB_TOKEN",
            allow_endpoints=["GET:/repos/*/pulls"],
        )
        assert scope.as_spec_dict() == {
            "service": "github",
            "host": "api.github.com",
            "credential_key": "env://GITHUB_TOKEN",
            "allow_endpoints": ["GET:/repos/*/pulls"],
        }

    def test_from_dict_parses(self):
        scope = CredentialScope.from_dict(
            {
                "service": "github",
                "host": "api.github.com",
                "credential_key": "env://GITHUB_TOKEN",
                "allow_endpoints": ["GET:/repos/*/pulls"],
            }
        )
        assert scope.service == "github"
        assert scope.host == "api.github.com"

    def test_from_dict_empty_endpoints_rejected(self):
        with pytest.raises(ValueError, match="allow_endpoints"):
            CredentialScope.from_dict(
                {
                    "service": "github",
                    "host": "api.github.com",
                    "credential_key": "env://GITHUB_TOKEN",
                    "allow_endpoints": [],
                }
            )

    def test_malformed_endpoint_rejected(self):
        with pytest.raises(ValueError, match="METHOD:/path"):
            CredentialScope(
                service="github",
                host="api.github.com",
                credential_key="env://GITHUB_TOKEN",
                allow_endpoints=["GET/repos"],  # missing ':' and leading '/'
            )


class TestCompileNonoFlags:
    def test_single_scope_single_endpoint_emits_all_three_flag_families(self):
        scopes = [
            CredentialScope(
                service="github",
                host="api.github.com",
                credential_key="env://GITHUB_TOKEN",
                allow_endpoints=["GET:/repos/*/pulls"],
            )
        ]
        assert compile_nono_flags(scopes) == [
            "--credential", "github",
            "--allow-endpoint", "github:GET:/repos/*/pulls",
            "--allow-domain", "https://api.github.com/repos/*/pulls",
        ]

    def test_multiple_endpoints_and_scopes_preserve_order(self):
        scopes = [
            CredentialScope(
                service="github",
                host="api.github.com",
                credential_key="env://GITHUB_TOKEN",
                allow_endpoints=["GET:/repos/*/pulls", "GET:/repos/*/issues"],
            ),
            CredentialScope(
                service="slack",
                host="slack.com",
                credential_key="env://SLACK_TOKEN",
                allow_endpoints=["POST:/api/chat.postMessage"],
            ),
        ]
        assert compile_nono_flags(scopes) == [
            "--credential", "github",
            "--allow-endpoint", "github:GET:/repos/*/pulls",
            "--allow-endpoint", "github:GET:/repos/*/issues",
            "--allow-domain", "https://api.github.com/repos/*/pulls",
            "--allow-domain", "https://api.github.com/repos/*/issues",
            "--credential", "slack",
            "--allow-endpoint", "slack:POST:/api/chat.postMessage",
            "--allow-domain", "https://slack.com/api/chat.postMessage",
        ]

    def test_same_path_different_methods_dedupes_allow_domain(self):
        scopes = [
            CredentialScope(
                service="github",
                host="api.github.com",
                credential_key="env://GITHUB_TOKEN",
                allow_endpoints=["GET:/repos/*/pulls", "POST:/repos/*/pulls"],
            )
        ]
        assert compile_nono_flags(scopes) == [
            "--credential", "github",
            "--allow-endpoint", "github:GET:/repos/*/pulls",
            "--allow-endpoint", "github:POST:/repos/*/pulls",
            "--allow-domain", "https://api.github.com/repos/*/pulls",
        ]

    def test_empty_scopes_yields_empty_flags(self):
        assert compile_nono_flags([]) == []


# Verbatim startup sample from docs/spikes/nono-proxy-runtime.md (nono 0.70.0).
_STARTUP_SAMPLE = (
    "  nono proxy listening on 127.0.0.1:61928\n"
    "  proxy URL: http://nono:b7cf3e2b734fa9@127.0.0.1:61928\n"
    "  token:     b7cf3e2b734fa9\n"
    "  export HTTPS_PROXY=http://nono:b7cf3e2b734fa9@127.0.0.1:61928\n"
    "  routes:\n"
    "    https://api.github.com | creds: env://GITHUB_TOKEN \u2713 | intercept: on | endpoint_rules: 1\n"
    "  TLS interception trust bundle: /Users/x/.local/state/nono/sessions/intercept-1-2/intercept-ca.pem\n"
    "  Press Ctrl-C to stop.\n"
)

# Verbatim -vv --log-file sample from the spike doc.
_AUDIT_SAMPLE = (
    '2026-07-30T14:27:01.250061Z  INFO l7 endpoint policy decision mode=connect_intercept '
    'target="api.github.com" method="GET" path="/repos/foo/bar/pulls" decision=Allow '
    'endpoint_policy_action="allow" endpoint_policy_rule="endpoint_policy.allow[* /repos/*/pulls]"\n'
    '2026-07-30T14:27:01.250072Z  INFO l7 endpoint policy decision mode=connect_intercept '
    'target="api.github.com" method="GET" path="/repos/foo/bar/pulls" decision=Allow '
    'endpoint_policy_action="allow" endpoint_policy_rule="endpoint_policy.allow[GET /repos/*/pulls]"\n'
    '2026-07-30T14:27:01.647966Z  INFO l7 proxy response mode=connect_intercept '
    'target="api.github.com" method="GET" path="/repos/foo/bar/pulls" status=401\n'
    '2026-07-30T14:27:01.681666Z  WARN tls_intercept: endpoint rules denied GET '
    '/repos/foo/bar/issues: no rule matched on api.github.com:443\n'
    '2026-07-30T14:27:01.681677Z  INFO proxy request denied mode=connect_intercept '
    'host="api.github.com" port=443 decision="deny" reason="endpoint rules denied GET '
    '/repos/foo/bar/issues: no rule matched on api.github.com:443"\n'
)


class TestParseConnectionInfo:
    def test_parses_url_token_port_ca_from_startup_output(self):
        info = parse_connection_info(_STARTUP_SAMPLE)
        assert info == {
            "url": "http://nono:b7cf3e2b734fa9@127.0.0.1:61928",
            "port": 61928,
            "token": "b7cf3e2b734fa9",
            "ca_path": "/Users/x/.local/state/nono/sessions/intercept-1-2/intercept-ca.pem",
        }

    def test_incomplete_output_raises(self):
        with pytest.raises(RuntimeError, match="connection info"):
            parse_connection_info("nothing useful here\n")


class TestProxyEnv:
    def test_proxy_env_uses_nono_url_verbatim_and_ca_vars(self):
        handle = ProxyHandle(
            process=None,
            url="http://nono:abc123@127.0.0.1:54321",
            port=54321,
            token="abc123",
            ca_path="/tmp/ca.pem",
            audit_path="/tmp/audit.log",
        )
        assert handle.proxy_env() == {
            "HTTP_PROXY": "http://nono:abc123@127.0.0.1:54321",
            "HTTPS_PROXY": "http://nono:abc123@127.0.0.1:54321",
            "http_proxy": "http://nono:abc123@127.0.0.1:54321",
            "https_proxy": "http://nono:abc123@127.0.0.1:54321",
            "NODE_EXTRA_CA_CERTS": "/tmp/ca.pem",
            "SSL_CERT_FILE": "/tmp/ca.pem",
            "CURL_CA_BUNDLE": "/tmp/ca.pem",
            "GIT_SSL_CAINFO": "/tmp/ca.pem",
        }


class TestAuditParse:
    def test_parses_allow_and_deny_records_deduped(self, tmp_path):
        log = tmp_path / "nono-proxy.log"
        log.write_text(_AUDIT_SAMPLE)
        handle = ProxyHandle(
            process=None, url="", port=0, token="", ca_path="",
            audit_path=str(log),
        )
        assert handle.audit() == [
            {"method": "GET", "path": "/repos/foo/bar/pulls", "allowed": True},
            {"method": "GET", "path": "/repos/foo/bar/issues", "allowed": False},
        ]

    def test_missing_log_returns_empty(self, tmp_path):
        handle = ProxyHandle(
            process=None, url="", port=0, token="", ca_path="",
            audit_path=str(tmp_path / "does-not-exist.log"),
        )
        assert handle.audit() == []


requires_nono = pytest.mark.skipif(
    shutil.which("nono") is None, reason="nono binary not installed"
)


class TestProxyEnvMerging:
    def test_jail_env_merges_proxy_env(self, tmp_path):
        from boundary.tools.sandbox import _jail_env

        env = _jail_env(
            tmp_path,
            proxy_env={
                "HTTPS_PROXY": "http://tok@127.0.0.1:5000",
                "SSL_CERT_FILE": "/tmp/ca.pem",
            },
        )
        assert env["HTTPS_PROXY"] == "http://tok@127.0.0.1:5000"
        assert env["SSL_CERT_FILE"] == "/tmp/ca.pem"

    def test_jail_env_without_proxy_env_unchanged(self, tmp_path, monkeypatch):
        from boundary.tools.sandbox import _jail_env

        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        env = _jail_env(tmp_path)
        assert "HTTPS_PROXY" not in env


class TestLiveReadWiring:
    def test_bash_reads_egress_and_proxy_env_at_call_time(self, monkeypatch, tmp_path):
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
            proxy_env = None

        agent = FakeAgent()
        reg = ToolRegistry()
        shell_mod.register_shell_tools(
            reg, Workspace(str(tmp_path)), driver="srt", agent=agent
        )
        # The runner sets these AFTER tool registration:
        agent.proxy_env = {"HTTPS_PROXY": "http://nono:tok@127.0.0.1:5000"}
        agent.egress_allowlist = ["127.0.0.1", "localhost"]

        reg.get("bash").call({"command": "echo hi", "reason": "x"})
        assert captured["proxy_env"] == {"HTTPS_PROXY": "http://nono:tok@127.0.0.1:5000"}
        assert captured["egress_allowlist"] == ["127.0.0.1", "localhost"]


@requires_nono
class TestProxyLifecycle:
    def test_start_ready_env_close(self, tmp_path):
        scopes = [
            CredentialScope(
                service="github",
                host="api.github.com",
                credential_key="env://GITHUB_TOKEN",
                allow_endpoints=["GET:/repos/*/pulls"],
            )
        ]
        handle = start_credential_proxy(scopes, ca_dir=str(tmp_path))
        try:
            assert handle.port > 0
            assert "127.0.0.1:" in handle.url
            assert handle.token
            env = handle.proxy_env()
            assert env["HTTPS_PROXY"].endswith(f"127.0.0.1:{handle.port}")
            assert env["SSL_CERT_FILE"] == handle.ca_path
        finally:
            handle.close()
        assert handle.process.poll() is not None
