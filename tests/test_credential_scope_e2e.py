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
