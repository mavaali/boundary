"""Envelope spec document — a versioned, hashable serialization of the policy.

`spec_dict()` is the envelope as a portable policy document; `spec_hash()` is
its canonical sha256. This is the artifact a run receipt signs against and a
non-runner frontend (CC plugin, MCP gateway) can compile from. Policy only:
pricing (token_rates) is excluded, so a rate-card update never changes the
hash of what the run was *allowed to do*.
"""
from __future__ import annotations

from boundary.envelope import Envelope


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
