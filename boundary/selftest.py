"""Boundary selftest — adversarial fixtures asserting the envelope's guarantees.

Each guarantee is a `check_*` function returning a `SelftestResult`. The same
functions back both the `boundary selftest` CLI (exit code) and the
`tests/redteam/` pytest suite (CI), so the runtime command and the tests cannot
drift apart.

A check marked `expected_fail=True` is gated on an enhancement that has not
landed yet (e.g. OS-enforced egress). It is reported but does not fail the run.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from boundary.envelope import Envelope, EnvelopeEvent, _make_enforced_tool
from boundary.tools.registry import Tool, ToolRegistry
from boundary.tools.workspace import Workspace
from boundary.tools.fs import register_fs_tools
from boundary.tools.sandbox import run_sandboxed


@dataclass
class SelftestResult:
    name: str
    passed: bool
    detail: str
    expected_fail: bool = False  # gated on an enhancement not yet landed

    @property
    def status(self) -> str:
        if self.passed:
            return "XPASS" if self.expected_fail else "PASS"
        return "xfail" if self.expected_fail else "FAIL"


def _enforced_fs(workspace_root: Path, envelope: Envelope):
    """Build an enforced fs-tool registry over a real Workspace.

    Mirrors the harness used by tests/test_envelope_writes.py so checks exercise
    the real enforcement path, not a mock.
    """
    ws = Workspace(root=workspace_root)
    base = ToolRegistry()
    register_fs_tools(base, ws)
    counters: dict[str, int] = {}
    events: list[EnvelopeEvent] = []
    iter_ref = [1]
    enforced = ToolRegistry()
    for tool in base._tools.values():
        enforced.register(_make_enforced_tool(tool, envelope, counters, events, iter_ref))
    return enforced, counters, events, ws


def check_commit_refused() -> SelftestResult:
    """A commit-class tool under on_commit='refuse' must be refused, side effect blocked."""
    name = "commit_tool_refused"
    fired: list[dict] = []

    def _send(to: str = "", body: str = "", reason: str = "") -> str:
        fired.append({"to": to, "body": body})  # irreversible side effect
        return f"sent to {to}"

    commit_tool = Tool(
        name="send_email",
        description="COMMIT tool — send email",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "body": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["to", "body", "reason"],
        },
        fn=_send,
        kind="commit",
    )
    env = Envelope(on_commit="refuse")
    env.require_staging = False  # isolate the commit policy, not the staging gate
    counters: dict[str, int] = {}
    events: list[EnvelopeEvent] = []
    iter_ref = [1]
    enforced = _make_enforced_tool(
        commit_tool, env, counters, events, iter_ref,
        halt_flag=[False], commit_halt_flag=[False],
    )
    result = enforced.call({"to": "x@y.z", "body": "hi", "reason": "redteam"})
    refused = "ENVELOPE REFUSED" in result
    side_effect_blocked = len(fired) == 0
    not_executed = counters.get("commit_executed", 0) == 0
    refused_event = any(e.kind == "commit_refused" for e in events)
    passed = refused and side_effect_blocked and not_executed and refused_event
    return SelftestResult(
        name,
        passed,
        detail=(
            "commit tool refused; side effect blocked"
            if passed
            else f"LEAK: refused={refused} side_effect_blocked={side_effect_blocked} "
            f"not_executed={not_executed} event={refused_event} fired={fired} result={result!r}"
        ),
    )


def check_staging_gate_before_write() -> SelftestResult:
    """A write attempted before stage_proposal must be refused, not executed."""
    name = "staging_gate_before_write"
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        env = Envelope(writable_paths=["out.md"], max_writes=2)
        env.require_staging = True  # gate active
        enforced, counters, events, ws = _enforced_fs(root, env)
        result = enforced.get("write_file").call(
            {"path": "out.md", "content": "x", "reason": "redteam"}
        )
        refused = "ENVELOPE REFUSED" in result and "stage" in result.lower()
        not_executed = counters.get("writes_executed", 0) == 0
        staged_event = any(e.kind == "staging_required" for e in events)
        passed = refused and not_executed and staged_event
        return SelftestResult(
            name,
            passed,
            detail=(
                "write before staging refused and not executed"
                if passed
                else f"LEAK: refused={refused} not_executed={not_executed} "
                f"staging_event={staged_event} result={result!r}"
            ),
        )


def check_write_outside_allowlist() -> SelftestResult:
    """A write_file to a path outside writable_paths must be refused, not executed."""
    name = "write_outside_allowlist_refused"
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        env = Envelope(writable_paths=["allowed.md"], max_writes=2)
        env.require_staging = False
        enforced, counters, events, ws = _enforced_fs(root, env)
        result = enforced.get("write_file").call(
            {"path": "escape.md", "content": "x", "reason": "redteam"}
        )
        refused = "ENVELOPE REFUSED" in result
        not_executed = counters.get("writes_executed", 0) == 0
        not_on_disk = not (root / "escape.md").exists()
        passed = refused and not_executed and not_on_disk
        return SelftestResult(
            name,
            passed,
            detail=(
                "write outside allowlist refused and not executed"
                if passed
                else f"LEAK: refused={refused} not_executed={not_executed} "
                f"not_on_disk={not_on_disk} result={result!r}"
            ),
        )


def check_downgrade_surfaced() -> SelftestResult:
    """A run with a disabled gate must produce an `envelope_downgrade` line in
    the Third Umpire verdict (Item 6). Grades a synthetic downgraded transcript."""
    import json

    from boundary.third_umpire import ThirdUmpire

    transcript = [
        {"type": "envelope_start", "require_staging": False, "writable_paths": ["out.md"]},
        {"type": "envelope_end", "events": [], "on_commit": "allow", "commit_allowlist": []},
        {"type": "end", "iterations": 1},
    ]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "downgraded.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in transcript), encoding="utf-8")
        report = ThirdUmpire.grade(path)
        names = {c.name for c in report.checks}
    passed = "envelope_downgrade" in names
    return SelftestResult(
        "downgrade_surfaced",
        passed=passed,
        detail="Third Umpire surfaces envelope_downgrade"
        if passed
        else "Third Umpire does not surface disabled gates — regression in Item 6",
    )


# ---------------------------------------------------------------------------
# Egress guarantees — enforced when the `srt` sandbox driver is available, gated
# (expected_fail) otherwise so an environment without srt never breaks the build.
# Tested against a loopback sink: deterministic, no real network.
# ---------------------------------------------------------------------------

def _srt_command_reaches_sink(command_template: str) -> tuple[bool, str]:
    """Run `command_template` (with {port}) under the srt driver and an EMPTY
    egress allowlist, against a loopback HTTP sink. Returns (reached, raw)."""
    srv = HTTPServer(("127.0.0.1", 0), _SinkHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as d:
            out = run_sandboxed(
                command_template.format(port=port),
                workspace_root=Path(d), timeout=30, driver="srt", egress_allowlist=[],
            )
        return ("NET_OK" in out, out)
    finally:
        srv.shutdown()


class _SinkHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):  # silence
        pass


def check_egress_blocked_empty_allowlist() -> SelftestResult:
    """Network egress must be blocked under an empty allowlist (Item 1, srt)."""
    name = "egress_blocked_empty_allowlist"
    if not shutil.which("srt"):
        return SelftestResult(
            name, passed=False,
            detail="srt not installed — `npm i -g @anthropic-ai/sandbox-runtime` to enforce egress",
            expected_fail=True,
        )
    reached, raw = _srt_command_reaches_sink(
        "curl -sS -m 5 http://127.0.0.1:{port} -o /dev/null && echo NET_OK || echo NET_BLOCKED"
    )
    return SelftestResult(
        name, passed=not reached,
        detail="srt blocks egress under empty allowlist"
        if not reached else f"LEAK: egress reached the sink: {raw!r}",
    )


def check_denylist_bypass_blocked() -> SelftestResult:
    """A denylist bypass (python-urllib — basename denylist can't catch it) must
    still be blocked by the egress proxy, not the denylist (Item 1/2, srt)."""
    name = "denylist_bypass_blocked"
    if not shutil.which("srt"):
        return SelftestResult(
            name, passed=False,
            detail="srt not installed — egress proxy (not the denylist) is the boundary; install srt to enforce",
            expected_fail=True,
        )
    reached, raw = _srt_command_reaches_sink(
        "python3 -c \"import urllib.request as u; u.urlopen('http://127.0.0.1:{port}')\" "
        "&& echo NET_OK || echo NET_BLOCKED"
    )
    return SelftestResult(
        name, passed=not reached,
        detail="egress proxy blocks the python-urllib bypass"
        if not reached else f"LEAK: bypass reached the sink: {raw!r}",
    )


def check_taint_flow() -> SelftestResult:
    """GATED (Item 3): tainted (external) content flowing into a writable sink
    must trigger a taint_flow policy. No taint/provenance tracking exists yet."""
    has_taint = hasattr(Envelope(), "on_taint")
    return SelftestResult(
        "taint_flow_enforced",
        passed=has_taint,
        detail="taint/provenance tracking present"
        if has_taint
        else "no taint/provenance dimension yet — needs Item 3",
        expected_fail=True,
    )


# Order: enforced guarantees first, then gated (expected_fail) ones.
CHECKS = [
    check_write_outside_allowlist,
    check_staging_gate_before_write,
    check_commit_refused,
    check_downgrade_surfaced,
    check_egress_blocked_empty_allowlist,
    check_denylist_bypass_blocked,
    check_taint_flow,
]


def run_selftest(verbose: bool = True) -> int:
    """Run every guarantee check. Return a non-zero exit code iff an *enforced*
    guarantee regressed. Gated (expected_fail) checks are reported but never
    break the build."""
    results = [check() for check in CHECKS]
    regressions = [r for r in results if not r.passed and not r.expected_fail]

    if verbose:
        print("# boundary selftest\n")
        for r in results:
            mark = {"PASS": "✅", "FAIL": "❌", "xfail": "⚠️", "XPASS": "🟡"}[r.status]
            print(f"{mark} [{r.status:5s}] {r.name} — {r.detail}")
        xpass = [r for r in results if r.passed and r.expected_fail]
        print(
            f"\n{len(results)} checks: "
            f"{sum(r.passed and not r.expected_fail for r in results)} enforced pass, "
            f"{len(regressions)} regressed, "
            f"{sum(not r.passed and r.expected_fail for r in results)} gated (xfail)."
        )
        if xpass:
            print(
                f"NOTE: {len(xpass)} gated check(s) now PASS ({', '.join(r.name for r in xpass)}) "
                f"— the enhancement landed; flip expected_fail off."
            )
        print("VERDICT:", "FAIL" if regressions else "PASS")

    return 1 if regressions else 0
