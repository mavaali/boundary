"""Typed `gh` commit tools.

`gh` is on the bash commit-denylist, so raw `gh` via `bash` is refused as
commit-class. This module is the sanctioned alternative the denylist comment
names explicitly: typed `kind="commit"` tools governed by the envelope's
`on_commit` policy (refuse / queue / ask / allow + allowlist), *not* a longer
denylist. Each runs through the same sandbox as `bash_commit`.

`gh_pr_create` appends the run's **policy attestation** — a `boundary.receipt/v1`
document — to the PR body, so an agent-authored PR describes the exact envelope
it was produced under. At commit time the run is not yet graded, so the
attestation carries the policy (`spec_hash` + `spec`) with `verdict: null` and a
pointer to `boundary receipt verify <run-id>` for the graded receipt emitted
after the run.

SLOPE GUARDRAIL — read before adding a verb:
  - HARD CAP: GH_VERB_CAP typed gh verbs. `gh` has a huge surface; the answer to
    "just one more" past the cap is an MCP gateway (v3 Item 4), NOT more tools
    here. Same discipline as the bash denylist's 12-entry cap.
"""
from __future__ import annotations

import json
import shlex
from collections.abc import Callable

from boundary.tools.registry import ToolRegistry
from boundary.tools.sandbox import run_sandboxed
from boundary.tools.workspace import Workspace

# Hard cap on the number of typed gh verbs (see SLOPE GUARDRAIL above).
GH_VERB_CAP = 3

RECEIPT_SCHEMA = "boundary.receipt/v1"


def _attestation_block(policy_provider: Callable[[], dict | None] | None) -> str:
    """A fenced boundary.receipt/v1 policy attestation for a PR body.

    Provisional by nature: the verdict is unknown until the run is graded, so
    this carries the policy and points at `boundary receipt verify` for the
    graded receipt.
    """
    policy = policy_provider() if policy_provider else None
    policy = policy or {}
    stub = {
        "schema": RECEIPT_SCHEMA,
        "spec_hash": policy.get("spec_hash"),
        "spec": policy.get("spec"),
        "verdict": None,
        "note": (
            "Provisional policy attestation from a Boundary-enveloped run. The "
            "graded receipt is emitted after the run; verify it with "
            "`boundary receipt verify <run-id>`."
        ),
    }
    return (
        "\n\n---\n"
        "_Produced by a Boundary-enveloped run — policy attestation below._\n\n"
        "```json\n" + json.dumps(stub, indent=2) + "\n```\n"
    )


def register_gh_tools(
    registry: ToolRegistry,
    workspace: Workspace,
    *,
    timeout: int = 60,
    driver: str = "seatbelt",
    egress_allowlist: list[str] | None = None,
    policy_provider: Callable[[], dict | None] | None = None,
) -> None:
    """Register the typed gh commit tools. `policy_provider` returns the live
    envelope policy `{spec_hash, spec}` for the receipt attestation (bound by
    the runner); None renders the attestation with a null policy."""

    def _run(command: str) -> str:
        return run_sandboxed(
            command,
            workspace_root=workspace.root,
            timeout=timeout,
            driver=driver,
            egress_allowlist=egress_allowlist,
        )

    @registry.add(
        "gh_pr_create",
        "Open a GitHub pull request (COMMIT tool — irreversible external action, "
        "gated by the envelope's on_commit policy). The run's policy attestation "
        "(boundary.receipt/v1) is appended to the PR body automatically. Include "
        "'reason' describing why this PR should exist.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "base": {"type": "string", "description": "base branch (optional)"},
                "draft": {"type": "boolean", "description": "open as draft (optional)"},
                "reason": {"type": "string", "description": "Why this PR should exist. Required."},
            },
            "required": ["title", "body", "reason"],
        },
        kind="commit",
    )
    def gh_pr_create(title: str, body: str, reason: str = "",
                     base: str | None = None, draft: bool = False) -> str:
        full_body = body + _attestation_block(policy_provider)
        parts = ["gh", "pr", "create", "--title", title, "--body", full_body]
        if base:
            parts += ["--base", base]
        if draft:
            parts += ["--draft"]
        return _run(shlex.join(parts))

    @registry.add(
        "gh_issue_comment",
        "Post a comment on a GitHub issue or PR (COMMIT tool — irreversible "
        "external action, gated by the envelope's on_commit policy). Include "
        "'reason'.",
        {
            "type": "object",
            "properties": {
                "issue": {"type": "string", "description": "issue/PR number or URL"},
                "body": {"type": "string"},
                "reason": {"type": "string", "description": "Why this comment is needed. Required."},
            },
            "required": ["issue", "body", "reason"],
        },
        kind="commit",
    )
    def gh_issue_comment(issue: str, body: str, reason: str = "") -> str:
        return _run(shlex.join(["gh", "issue", "comment", issue, "--body", body]))
