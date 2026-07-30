import pytest

from boundary.credential_proxy import CredentialScope


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
