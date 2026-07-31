"""Third Umpire credential_scope_held: attest a scoped run was enforced.

A run that declared credential_scopes is graded on whether the nono sandbox
enforced them (credential_scopes_enforced). Declared-but-not-enforced is a fail
(the credential was unbounded). No scopes -> no check (consistent with
egress_uncontained: only report the dimensions in play).
"""
from __future__ import annotations

import json

from boundary.third_umpire import ThirdUmpire

_SCOPE = {
    "service": "github",
    "host": "api.github.com",
    "credential_key": "env://GITHUB_TOKEN",
    "allow_endpoints": ["GET:/repos/*/pulls"],
}


def _tx(tmp_path, *, credential_scopes, enforced):
    records = [
        {"type": "envelope_start", "writable_paths": ["out.md"],
         "require_staging": False, "max_writes": 10,
         "credential_scopes": credential_scopes},
        {"type": "assistant", "iteration": 1, "content": "[DATA] done", "tool_calls": []},
        {"type": "envelope_end", "writes_attempted": 1, "writes_executed": 1,
         "external_calls": 0, "commit_attempted": 0, "commit_executed": 0,
         "input_tokens": 1000, "output_tokens": 500, "estimated_dollars": 0.01,
         "sandbox_driver": "nono", "credential_scopes_enforced": enforced,
         "events": []},
        {"type": "end", "iterations": 2},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _held(report):
    return next((c for c in report.checks if c.name == "credential_scope_held"), None)


class TestCredentialScopeHeld:
    def test_no_scopes_no_check(self, tmp_path):
        report = ThirdUmpire.grade(_tx(tmp_path, credential_scopes=[], enforced=False))
        assert _held(report) is None

    def test_scopes_enforced_passes(self, tmp_path):
        report = ThirdUmpire.grade(_tx(tmp_path, credential_scopes=[_SCOPE], enforced=True))
        result = _held(report)
        assert result is not None
        assert result.passed is True
        assert result.severity == "info"

    def test_scopes_declared_but_not_enforced_fails(self, tmp_path):
        report = ThirdUmpire.grade(_tx(tmp_path, credential_scopes=[_SCOPE], enforced=False))
        result = _held(report)
        assert result is not None
        assert result.passed is False
        assert result.severity == "fail"
        assert "not" in result.detail.lower()
