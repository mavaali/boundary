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
