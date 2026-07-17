"""Policy kernel — transport-agnostic envelope enforcement.

The kernel is the decision core of the envelope: given a policy (the Envelope
dataclass, duck-typed here) and a stream of typed tool events, it returns
Decisions and records EnvelopeEvents. It holds no transport: no model clients,
no agent loop, no HTTP. The runner (EnvelopeRunner) is one frontend; a Claude
Code governor or MCP gateway can consume the same kernel without the runner.

Enforcement is split around tool execution:
    pre_tool(name, kind, args)   -> Decision   (refusals, halts, pre-side state)
    <frontend executes the tool if Decision.action == "allow">
    post_tool(decision, name, args, result=..., error=...)
                                  (success-only accounting + events)

State (counters, events, iter_ref) is intentionally caller-shareable so
existing harnesses that inspect counters/events keep working unchanged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# -----------------------------------------------------------------------------
# COMMIT-TOOL KILL-LIST
# -----------------------------------------------------------------------------
# A "commit" tool is an irreversible external side effect: send email, post
# Teams message, push commit, file ADO bug, submit expense report.
#
# Bash can sidestep typed commit tools trivially (`curl -X POST ...`, `gh
# issue create`, `osascript`). We close the obvious paths with a basename
# denylist: if bash's command starts with any of these binaries (basename of
# argv[0]), the call is refused with instructions to use `bash_commit` instead.
#
# SLOPE GUARDRAILS — read before adding entries:
#   - HARD CAP: 12 entries. At 13, stop and reconsider the model, don't extend.
#   - NO REGEX, NO ARGUMENT INSPECTION except the single sanctioned `git`
#     subcommand exception below.
#   - If you find yourself wanting argv inspection on a SECOND binary, build a
#     typed `kind="commit"` tool instead. That's why bash_commit exists.
#   - The Third Umpire surfaces every bash_commit / commit-tool call in its verdict. If an
#     agent shells out to `gh` repeatedly, the answer is a typed gh_* commit
#     tool, NOT a longer denylist.
# -----------------------------------------------------------------------------
BASH_COMMIT_DENYLIST: tuple[str, ...] = (
    "curl", "wget", "gh", "az", "mail", "sendmail", "osascript", "git",
)
# The ONLY sanctioned argv-inspection exception. `git status` / `git log` are
# read; `git push` / `git commit` are commit. If a second binary ever needs
# this, that's the signal to rethink the model.
_GIT_COMMIT_SUBCOMMANDS: frozenset[str] = frozenset({"push", "commit", "tag"})


def bash_command_is_commit(command: str) -> tuple[bool, str]:
    """Inspect a bash command string. Return (is_commit, reason).

    Pure basename match on the first token. The single `git` exception
    inspects argv[1] against _GIT_COMMIT_SUBCOMMANDS. No regex anywhere.
    """
    if not command or not command.strip():
        return False, ""
    parts = command.strip().split()
    head = parts[0]
    # Strip env-var assignments (FOO=bar curl ...) — take next token.
    while "=" in head and not head.startswith("/") and not head.startswith("."):
        if len(parts) < 2:
            return False, ""
        parts = parts[1:]
        head = parts[0]
    # Basename only; ignore path prefix.
    base = os.path.basename(head)
    if base not in BASH_COMMIT_DENYLIST:
        return False, ""
    if base == "git":
        sub = parts[1] if len(parts) > 1 else ""
        if sub not in _GIT_COMMIT_SUBCOMMANDS:
            return False, ""
        return True, f"git {sub}"
    return True, base


@dataclass
class EnvelopeEvent:
    kind: str  # "staged" | "staging_required" | "write_allowed" | "write_refused" | "write_failed" | "missing_reason" | "ambiguity_halt" | "limit_hit" | "commit_refused" | "commit_allowed" | "commit_halt" | "bash_commit_blocked" | "taint_flow" | "taint_inherited"
    tool: str
    detail: str
    iteration: int


@dataclass
class Decision:
    """Outcome of pre_tool. `action` tells the frontend what to do:

    - "allow"       execute the tool, then call post_tool with the outcome
    - "refuse"      do not execute; return `message` to the agent
    - "halt_queue"  do not execute; return `message`; stop the run and queue review
    - "halt_ask"    do not execute; return `message`; stop the run for ask_human

    `accounting` carries which success-only counter post_tool should bump:
    None | "append" | "write" | "bash" | "commit".
    """
    action: str
    message: str | None = None
    accounting: str | None = None


class PolicyKernel:
    def __init__(
        self,
        envelope: Any,
        counters: dict | None = None,
        events: list[EnvelopeEvent] | None = None,
        iter_ref: list[int] | None = None,
        provenance: Any = None,
    ):
        self.envelope = envelope
        self.counters = counters if counters is not None else {}
        self.events = events if events is not None else []
        self.iter_ref = iter_ref if iter_ref is not None else [0]
        # Optional cross-run taint oracle (v2 Item 1): Callable[[str], dict|None]
        # mapping a read path to {run_id, schedule_name, sources} when the
        # file's most recent writer run was tainted and not human-cleared.
        self.provenance = provenance

    # -- helpers -------------------------------------------------------------

    def _event(self, kind: str, tool: str, detail: str) -> None:
        self.events.append(EnvelopeEvent(kind, tool, detail, self.iter_ref[0]))

    @staticmethod
    def _is_error_result(r: Any) -> bool:
        return isinstance(r, str) and r.startswith("ERROR:")

    def record_stage(
        self,
        thesis: str,
        intended_write: str | None = None,
        cost_class: str = "unknown",
    ) -> None:
        """Mark the run staged (the staging pivot). Records the taint set that
        fed the thesis/evidence so far."""
        c = self.counters
        c["staged"] = 1
        c["stage_calls"] = c.get("stage_calls", 0) + 1
        taint = c.get("tainted_reads", 0)
        taint_note = f" taint={taint}({c.get('tainted_sources', [])[:3]})" if taint else ""
        summary = (
            f"thesis={thesis[:120]} cost_class={cost_class} "
            f"intended_write={intended_write or '(none)'}{taint_note}"
        )
        self._event("staged", "stage_proposal", summary)

    # -- pre-execution decision ------------------------------------------------

    def pre_tool(self, name: str, kind: str, args: dict) -> Decision:
        env = self.envelope
        c = self.counters
        staging_required = env.require_staging and bool(env.writable_paths)

        if staging_required and name == "read_file" and c.get("staged", 0) == 0:
            c["unstaged_reads"] = c.get("unstaged_reads", 0) + 1
            if c["unstaged_reads"] > env.max_unstaged_reads:
                self._event(
                    "staging_required", name,
                    f"unstaged_reads={c['unstaged_reads']} max={env.max_unstaged_reads}",
                )
                return Decision("refuse", (
                    "ENVELOPE REFUSED: orientation reads are exhausted. "
                    "Call `stage_proposal` with a tentative answer, hypotheses, "
                    "evidence plan, intended write, cost class, and kill criteria "
                    "before doing more deep reads. Anti-boil-the-ocean rule: every "
                    "next read must test or revise the staged answer."
                ))

        if (
            staging_required
            and c.get("staged", 0) == 0
            and (kind in ("write", "commit") or name == "bash")
        ):
            self._event("staging_required", name, "write_or_commit_before_stage")
            return Decision("refuse", (
                "ENVELOPE REFUSED: stage the candidate answer before writing or "
                "committing. Call `stage_proposal` first so write rejection resumes "
                "from staging instead of restarting research."
            ))

        # 1. Reason check
        if env.require_reason and kind in ("write", "external", "commit"):
            reason = args.get("reason", "").strip() if isinstance(args.get("reason"), str) else ""
            if not reason:
                self._event("missing_reason", name, "(no reason)")
                return Decision("refuse", (
                    f"ENVELOPE REFUSED: tool '{name}' is a {kind} tool — "
                    f"you must include a non-empty 'reason' field."
                ))

        # 1b. Taint gate — tainted (untrusted external) content flowing into a
        #     writable sink. Coarse: once any tainted read has happened, every
        #     write/commit is a potential exfil channel.
        if (
            env.on_taint != "allow"
            and c.get("tainted_reads", 0) > 0
            and (kind in ("write", "commit") or name == "bash")
        ):
            sources = c.get("tainted_sources", [])
            self._event(
                "taint_flow", name,
                f"on_taint={env.on_taint} tainted_reads={c['tainted_reads']} "
                f"sources={sources[:3]}",
            )
            if env.on_taint == "refuse":
                return Decision("refuse", (
                    f"ENVELOPE REFUSED: this run read untrusted external content "
                    f"(taint sources: {sources[:3]}) and a write to a shared/writable path "
                    f"is a potential exfiltration channel (on_taint=refuse). Do not route "
                    f"untrusted content into a write. If this flow is intentional and safe, "
                    f"re-scope with on_taint=warn, or stage only trusted/derived content."
                ))
            # warn: record the flow, let the write proceed.

        # 2. append_file — continuation of a prior write_file. Counted against
        #    max_appends, NOT max_writes.
        if name == "append_file":
            path = args.get("path", "")
            if not env.path_allowed(path):
                self._event("write_refused", name, f"path={path}")
                return Decision("refuse", (
                    f"ENVELOPE REFUSED: path '{path}' is not in writable_paths "
                    f"{env.writable_paths}."
                ))
            if c.get("appends_executed", 0) >= env.max_appends:
                self._event("limit_hit", name, f"max_appends={env.max_appends}")
                return Decision("refuse", f"ENVELOPE REFUSED: max_appends ({env.max_appends}) reached.")
            return Decision("allow", accounting="append")

        # 3. write_file / edit_file — count only on success. Failed attempts
        #    bump writes_attempted but NOT writes_executed.
        if kind == "write" and name in ("write_file", "edit_file"):
            c["writes_attempted"] = c.get("writes_attempted", 0) + 1
            path = args.get("path", "")
            if not env.path_allowed(path):
                self._event("write_refused", name, f"path={path}")
                return Decision("refuse", (
                    f"ENVELOPE REFUSED: path '{path}' is not in writable_paths "
                    f"{env.writable_paths}. Either confirm with the user (call ask_human) "
                    f"or write only to allowed paths."
                ))
            if c.get("writes_executed", 0) >= env.max_writes:
                self._event("limit_hit", name, f"max_writes={env.max_writes}")
                return Decision("refuse", (
                    f"ENVELOPE REFUSED: max_writes ({env.max_writes}) reached. "
                    f"If you need to extend an existing write, use append_file (counted "
                    f"against max_appends={env.max_appends}, not max_writes)."
                ))
            return Decision("allow", accounting="write")

        # 4. Bash — counts as a write iff envelope.allow_bash.
        if name == "bash":
            if not env.allow_bash:
                return Decision("refuse", "ENVELOPE REFUSED: bash is disabled for this run.")
            cmd_str = args.get("command", "") if isinstance(args.get("command"), str) else ""
            is_commit, matched = bash_command_is_commit(cmd_str)
            if is_commit:
                self._event(
                    "bash_commit_blocked", name,
                    f"matched={matched} command={cmd_str[:120]}",
                )
                return Decision("refuse", (
                    f"ENVELOPE REFUSED: bash command starts with '{matched}', "
                    f"which is on the commit-tool denylist (irreversible side effect). "
                    f"If this is intentional, call `bash_commit` with the same command "
                    f"and a 'reason' field. The run's commit policy will decide whether "
                    f"to allow, queue, ask, or refuse."
                ))
            c["writes_attempted"] = c.get("writes_attempted", 0) + 1
            if c.get("writes_executed", 0) >= env.max_writes:
                self._event("limit_hit", name, f"max_writes={env.max_writes}")
                return Decision("refuse", f"ENVELOPE REFUSED: max_writes ({env.max_writes}) reached.")
            return Decision("allow", accounting="bash")

        # 5. Commit-tool gate — irreversible external actions.
        if kind == "commit":
            c["commit_attempted"] = c.get("commit_attempted", 0) + 1
            policy = env.on_commit
            allowlist = env.commit_allowlist or []
            if policy == "allow" and (not allowlist or name in allowlist):
                # bash_commit gets the same success-only write accounting as a
                # write so it counts against max_writes.
                if name == "bash_commit":
                    c["writes_attempted"] = c.get("writes_attempted", 0) + 1
                    if c.get("writes_executed", 0) >= env.max_writes:
                        self._event("limit_hit", name, f"max_writes={env.max_writes}")
                        return Decision("refuse", f"ENVELOPE REFUSED: max_writes ({env.max_writes}) reached.")
                return Decision("allow", accounting="commit")
            if policy == "queue":
                self._event(
                    "commit_halt", name,
                    f"queued for human approval — args={list(args.keys())}",
                )
                return Decision("halt_queue", (
                    f"[HALTED] Commit tool '{name}' requires human approval. "
                    f"The run will stop and be queued for review. "
                    f"Args snapshot: {args}"
                ))
            if policy == "ask":
                self._event(
                    "commit_halt", name,
                    f"ask_human required — args={list(args.keys())}",
                )
                return Decision("halt_ask", (
                    f"[HALTED] Commit tool '{name}' requires explicit human "
                    f"confirmation. Call `ask_human` describing the intended action "
                    f"and the exact args, then re-attempt only if the human approves."
                ))
            # Default / "refuse" / unknown policy / "allow" but not in allowlist
            self._event("commit_refused", name, f"policy={policy} allowlist={allowlist}")
            if policy == "allow" and allowlist and name not in allowlist:
                return Decision("refuse", (
                    f"ENVELOPE REFUSED: commit tool '{name}' is not in "
                    f"this run's commit_allowlist {allowlist}. "
                    f"Either re-scope the task to avoid this commit, or stop and "
                    f"surface the gap via ask_human."
                ))
            return Decision("refuse", (
                f"ENVELOPE REFUSED: tool '{name}' is a commit tool "
                f"(irreversible external side effect) and this run's commit policy "
                f"is '{policy}'. Do not retry. If the task genuinely requires this "
                f"commit, stop and report via ask_human — a human must re-scope "
                f"the run with on_commit=allow + commit_allowlist=['{name}']."
            ))

        # 6. External rate cap + taint labeling
        if kind == "external":
            c["external_calls"] = c.get("external_calls", 0) + 1
            if c["external_calls"] > env.max_external:
                self._event("limit_hit", name, f"max_external={env.max_external}")
                return Decision("refuse", f"ENVELOPE REFUSED: max_external ({env.max_external}) reached.")
            # Taint labeling: external content is untrusted. Mark the run
            # tainted so a later write to a writable sink trips the taint gate.
            c["tainted_reads"] = c.get("tainted_reads", 0) + 1
            src = args.get("url") or args.get("query") or name
            c.setdefault("tainted_sources", []).append(str(src)[:80])

        # 7. Read-time taint inheritance (cross-run lineage, v2 Item 1) —
        #    reading a file whose most recent writer run was tainted inherits
        #    that taint. The read proceeds (labeled, not blocked); the same-run
        #    taint gate above then governs any later write.
        if name == "read_file" and self.provenance is not None:
            path = args.get("path", "")
            prov = self.provenance(path)
            if prov:
                c["tainted_reads"] = c.get("tainted_reads", 0) + 1
                c.setdefault("tainted_sources", []).append(
                    f"run:{prov.get('run_id')}:{path}"[:80])
                self._event(
                    "taint_inherited", name,
                    f"path={path} run={prov.get('run_id')} "
                    f"sources={prov.get('sources', [])[:2]}",
                )

        # 8. Default path — read tools and unmetered externals
        return Decision("allow")

    # -- post-execution accounting ----------------------------------------------

    def post_tool(
        self,
        decision: Decision,
        name: str,
        args: dict,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """Success-only accounting after the frontend executed an allowed tool.
        Call with `error=` if the tool raised (the frontend re-raises after)."""
        acct = decision.accounting
        if acct is None:
            return
        c = self.counters
        if error is not None:
            kind = "commit_refused" if acct == "commit" else "write_failed"
            self._event(kind, name, f"{type(error).__name__}: {error}")
            return
        if self._is_error_result(result):
            kind = "commit_refused" if acct == "commit" else "write_failed"
            self._event(kind, name, str(result)[:200])
            return
        if acct == "append":
            c["appends_executed"] = c.get("appends_executed", 0) + 1
            self._event("write_allowed", name, f"path={args.get('path', '')} (append)")
        elif acct == "write":
            c["writes_executed"] = c.get("writes_executed", 0) + 1
            self._event("write_allowed", name, f"path={args.get('path', '')}")
        elif acct == "bash":
            c["writes_executed"] = c.get("writes_executed", 0) + 1
            self._event("write_allowed", name, "bash")
        elif acct == "commit":
            c["commit_executed"] = c.get("commit_executed", 0) + 1
            if name == "bash_commit":
                c["writes_executed"] = c.get("writes_executed", 0) + 1
            self._event("commit_allowed", name, "policy=allow")
