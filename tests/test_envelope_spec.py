"""Envelope spec document — a versioned, hashable serialization of the policy.

`spec_dict()` is the envelope as a portable policy document; `spec_hash()` is
its canonical sha256. This is the artifact a run receipt signs against and a
non-runner frontend (CC plugin, MCP gateway) can compile from. Policy only:
pricing (token_rates) is excluded, so a rate-card update never changes the
hash of what the run was *allowed to do*.
"""
from __future__ import annotations

import shutil

import pytest

from boundary.credential_proxy import CredentialScope
from boundary.envelope import (
    CredentialScopePreconditionError,
    Envelope,
    check_credential_scope_preconditions,
)


def test_spec_serializes_versioned_policy():
    spec = Envelope(writable_paths=["out.md"], max_writes=3).spec_dict()
    assert spec["spec_version"] == 1
    assert spec["writable_paths"] == ["out.md"]
    assert spec["max_writes"] == 3
    # every enforcement-bearing dimension is present
    for key in ("on_commit", "on_taint", "require_staging", "require_srt_for_bash",
                "allow_bash", "write_profile", "repeat_halt", "max_dollars"):
        assert key in spec, key
    # pricing is not policy
    assert "token_rates" not in spec


def test_spec_hash_is_stable_across_instances():
    a = Envelope(writable_paths=["out.md"], max_writes=3)
    b = Envelope(writable_paths=["out.md"], max_writes=3)
    assert a.spec_hash() == b.spec_hash()
    assert len(a.spec_hash()) == 64  # sha256 hex


def test_spec_hash_changes_when_policy_changes():
    base = Envelope(writable_paths=["out.md"])
    assert base.spec_hash() != Envelope(writable_paths=["out.md"], on_taint="refuse").spec_hash()
    assert base.spec_hash() != Envelope(writable_paths=["other.md"]).spec_hash()
    assert base.spec_hash() != Envelope(writable_paths=["out.md"], max_writes=1).spec_hash()


def test_spec_hash_ignores_pricing_changes():
    a = Envelope(writable_paths=["out.md"])
    b = Envelope(writable_paths=["out.md"])
    b.token_rates = {"some-model": {"input": 1.0, "cached": 0.1, "output": 2.0}}
    assert a.spec_hash() == b.spec_hash()


class TestCredentialScopesField:
    def _scope(self):
        return CredentialScope(
            service="github",
            host="api.github.com",
            credential_key="env://GITHUB_TOKEN",
            allow_endpoints=["GET:/repos/*/pulls"],
        )

    def test_default_is_empty_list(self):
        assert Envelope().credential_scopes == []

    def test_spec_dict_includes_credential_scopes(self):
        spec = Envelope(credential_scopes=[self._scope()]).spec_dict()
        assert spec["credential_scopes"] == [
            {
                "service": "github",
                "host": "api.github.com",
                "credential_key": "env://GITHUB_TOKEN",
                "allow_endpoints": ["GET:/repos/*/pulls"],
            }
        ]

    def test_spec_hash_changes_with_credential_scopes(self):
        assert Envelope().spec_hash() != Envelope(
            credential_scopes=[self._scope()]
        ).spec_hash()


_SCOPES = [
    CredentialScope(
        service="github",
        host="api.github.com",
        credential_key="env://GITHUB_TOKEN",
        allow_endpoints=["GET:/repos/*/pulls"],
    )
]


class TestCredentialScopePreconditions:
    def test_no_scopes_no_check(self):
        check_credential_scope_preconditions([], resolved_driver="none")

    def test_refuses_when_nono_missing(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(CredentialScopePreconditionError, match="nono"):
            check_credential_scope_preconditions(_SCOPES, resolved_driver="srt")

    def test_refuses_when_driver_not_nono(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/nono")
        with pytest.raises(CredentialScopePreconditionError, match="nono"):
            check_credential_scope_preconditions(_SCOPES, resolved_driver="srt")

    def test_passes_with_nono_installed_and_nono_driver(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/nono")
        check_credential_scope_preconditions(_SCOPES, resolved_driver="nono")


class _StopClient:
    model = "claude-sonnet-4.6"

    def __init__(self, raise_exc=None):
        self._raise = raise_exc

    def chat(self, messages, tools=None, **kw):
        if self._raise is not None:
            raise self._raise
        from boundary.clients.base import ChatResponse, Message as M

        return ChatResponse(
            message=M(role="assistant", content="done", tool_calls=[]),
            finish_reason="stop",
            input_tokens=0, output_tokens=1, cached_input_tokens=0,
        )


class TestRunnerCredentialScopes:
    def _runner(self, monkeypatch, tmp_path, *, driver, transcript=False):
        import boundary.envelope as env_mod
        from boundary.agent import Agent

        # nono "installed" for the precondition; driver passed explicitly so no
        # auto-resolution touches the real binary.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
        agent = Agent(
            name="s", system_prompt="x", workspace=str(tmp_path),
            client=_StopClient(), enable_fs=True, enable_shell=False,
            enable_web=False, transcript=transcript, sandbox_driver=driver,
        )
        env = Envelope(
            writable_paths=["out.md"], require_staging=False, credential_scopes=_SCOPES,
        )
        return env_mod.EnvelopeRunner(agent, env)

    def test_scoped_run_under_nono_completes(self, monkeypatch, tmp_path):
        # Precondition passes (nono installed + driver nono); run completes.
        self._runner(monkeypatch, tmp_path, driver="nono").run("go")

    def test_scoped_run_refused_when_driver_not_nono(self, monkeypatch, tmp_path):
        runner = self._runner(monkeypatch, tmp_path, driver="srt")
        with pytest.raises(CredentialScopePreconditionError, match="nono"):
            runner.run("go")

    def test_envelope_end_logs_enforced_flag(self, monkeypatch, tmp_path):
        import json

        from boundary.transcript import Transcript

        tpath = tmp_path / "run.jsonl"
        self._runner(
            monkeypatch, tmp_path, driver="nono", transcript=Transcript(path=tpath)
        ).run("go")
        ends = [json.loads(line) for line in tpath.read_text().splitlines()
                if '"envelope_end"' in line]
        assert ends and ends[0].get("credential_scopes_enforced") is True


class TestParseCredentialScopeArg:
    def test_parses_single_endpoint(self):
        from boundary.cli import parse_credential_scope_arg

        scope = parse_credential_scope_arg(
            "service=github,host=api.github.com,key=env://GITHUB_TOKEN,"
            "endpoint=GET:/repos/*/pulls"
        )
        assert scope.service == "github"
        assert scope.host == "api.github.com"
        assert scope.credential_key == "env://GITHUB_TOKEN"
        assert scope.allow_endpoints == ["GET:/repos/*/pulls"]

    def test_parses_repeated_endpoints(self):
        from boundary.cli import parse_credential_scope_arg

        scope = parse_credential_scope_arg(
            "service=github,host=api.github.com,key=env://GITHUB_TOKEN,"
            "endpoint=GET:/repos/*/pulls,endpoint=GET:/repos/*/issues"
        )
        assert scope.allow_endpoints == ["GET:/repos/*/pulls", "GET:/repos/*/issues"]

    def test_missing_endpoint_rejected(self):
        from boundary.cli import parse_credential_scope_arg

        with pytest.raises(ValueError, match="allow_endpoints"):
            parse_credential_scope_arg(
                "service=github,host=api.github.com,key=env://GITHUB_TOKEN"
            )

    def test_missing_service_rejected(self):
        from boundary.cli import parse_credential_scope_arg

        with pytest.raises(ValueError, match="service"):
            parse_credential_scope_arg("host=api.github.com,key=env://X,endpoint=GET:/a")

    def test_missing_host_rejected(self):
        from boundary.cli import parse_credential_scope_arg

        with pytest.raises(ValueError, match="host"):
            parse_credential_scope_arg("service=x,key=env://X,endpoint=GET:/a")

    def test_unknown_key_rejected(self):
        from boundary.cli import parse_credential_scope_arg

        with pytest.raises(ValueError, match="unknown"):
            parse_credential_scope_arg(
                "service=x,host=h,key=env://X,endpoint=GET:/a,bogus=1"
            )

    def test_run_malformed_credential_scope_exits_2(self):
        from boundary.cli import main

        rc = main([
            "run", "--task", "x", "--credential-scope", "bogus",
            "--envelope-writable", "out.md",
        ])
        assert rc == 2
