"""Regression guards for two write-jail scope escapes from the security audit.

F2 — Envelope.path_allowed matched writable_paths with fnmatch on the raw path
     string, where `*` spans `/` and `..` is not neutralized, so "reports/*.md"
     authorized "reports/a/b/c.md" and "reports/../secret.md". Matching is now
     anchored, segment-aware, and traversal-safe.
F3 — the enforced-tool wrapper dispatched the write gate on tool *name*, so a
     kind="write" tool with an unrecognized name fell through to the unchecked
     default path (no path_allowed, no max_writes). Unhandled mutating tools are
     now refused fail-closed.
"""
from __future__ import annotations

from boundary.envelope import Envelope, _make_enforced_tool
from boundary.tools.registry import Tool

# --- F2: anchored, segment-aware, traversal-safe writable_paths ---------------


def test_glob_star_does_not_cross_slash():
    env = Envelope(writable_paths=["reports/*.md"])
    assert env.path_allowed("reports/weekly.md")
    assert not env.path_allowed("reports/a/b/c.md")


def test_parent_traversal_is_rejected():
    env = Envelope(writable_paths=["reports/*.md"])
    assert not env.path_allowed("reports/../secrets/creds.md")
    assert not env.path_allowed("reports/../../etc/passwd.md")


def test_bare_star_matches_only_top_level():
    env = Envelope(writable_paths=["*.md"])
    assert env.path_allowed("top.md")
    assert not env.path_allowed("a/b/c.md")


def test_double_star_is_opt_in_recursion():
    assert Envelope(writable_paths=["reports/**"]).path_allowed("reports/a/b/c.md")
    assert Envelope(writable_paths=["**/*.md"]).path_allowed("a/b/c.md")


def test_absolute_path_rejected():
    env = Envelope(writable_paths=["out.md"])
    assert not env.path_allowed("/etc/passwd")


def test_matching_is_case_sensitive():
    # fnmatchcase — the allowlist is not silently widened on case-insensitive fs.
    env = Envelope(writable_paths=["Reports/*.md"])
    assert env.path_allowed("Reports/x.md")
    assert not env.path_allowed("reports/x.md")


def test_preserved_behaviour():
    # literals, leading slash, empty list, and single-segment globs still work.
    assert Envelope(writable_paths=["out.md"]).path_allowed("out.md")
    assert Envelope(writable_paths=["out.md"]).path_allowed("/out.md")
    assert not Envelope(writable_paths=["allowed.md"]).path_allowed("elsewhere.md")
    assert not Envelope(writable_paths=[]).path_allowed("out.md")
    assert Envelope(writable_paths=["scratch/*.md"]).path_allowed("scratch/review.md")


# --- F3: unhandled mutating tools fail closed ---------------------------------


def _write_tool(recorder):
    def fn(path: str, reason: str = "") -> str:
        recorder.append(path)
        return "DID THE WRITE"
    return Tool(
        "patch_file", "hypothetical future write tool",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        fn, kind="write",
    )


def test_unknown_write_tool_is_refused_and_not_executed():
    recorder: list[str] = []
    env = Envelope(writable_paths=["out.md"], require_staging=False)
    enforced = _make_enforced_tool(_write_tool(recorder), env, {}, events := [], [0])
    r = enforced.fn(path="../../etc/passwd", reason="x")
    assert r.startswith("ENVELOPE REFUSED")
    assert recorder == []  # never executed
    assert any(e.kind == "write_refused" for e in events)


def test_unknown_write_tool_refused_even_for_in_scope_path():
    # The point is fail-closed regardless of path: the envelope can't bound a
    # tool it doesn't understand, so it refuses rather than trusting the path.
    recorder: list[str] = []
    env = Envelope(writable_paths=["out.md"], require_staging=False)
    enforced = _make_enforced_tool(_write_tool(recorder), env, {}, [], [0])
    r = enforced.fn(path="out.md", reason="x")
    assert r.startswith("ENVELOPE REFUSED")
    assert recorder == []
