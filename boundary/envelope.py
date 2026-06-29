"""Envelope primitives — read/write split, write-allowlist, annunciation, ambiguity halt.

The envelope is a pre-declared boundary the agent runs inside. It enforces at the
tool layer (not just the prompt layer) so a confused agent cannot interpolate past it.

Usage:
    envelope = Envelope(
        writable_paths=["scratch/banner-survey.md"],
        max_writes=3,
    )
    runner = EnvelopeRunner(agent, envelope)
    result = runner.run(task)
"""
from __future__ import annotations
import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from boundary.agent import Agent
from boundary.clients.base import Message
from boundary.loop import LoopResult, run_loop
from boundary.taint import TaintStore
from boundary.tools.registry import Tool, ToolRegistry


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


def _bash_command_is_commit(command: str) -> tuple[bool, str]:
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
    import os as _os
    base = _os.path.basename(head)
    if base not in BASH_COMMIT_DENYLIST:
        return False, ""
    if base == "git":
        sub = parts[1] if len(parts) > 1 else ""
        if sub not in _GIT_COMMIT_SUBCOMMANDS:
            return False, ""
        return True, f"git {sub}"
    return True, base


@dataclass
class Envelope:
    writable_paths: list[str] = field(default_factory=list)
    max_writes: int = 10
    min_writes: int = 1
    max_external: int = 20
    # Chunked-write continuation cap. append_file uses this, NOT max_writes,
    # so an agent can split one logical long write across many appends without
    # eating its write budget. Set 0 to disable chunked writes entirely.
    max_appends: int = 10
    require_reason: bool = True
    allow_bash: bool = True
    stop_on_ambiguity: bool = True
    budget_pressure_at: tuple[float, ...] = (0.6, 0.8)
    # No-progress / repeated-action detection (D). When the agent issues the same
    # tool call (name + canonical args) repeatedly it is stuck and burning budget
    # on unproductive exchanges (the ComPilot local-optima behavior). After
    # `repeat_warn` identical calls the agent is warned in-band; after
    # `repeat_halt` the run halts (stop_reason "no_progress_halt"). Set
    # repeat_halt=0 to disable.
    repeat_warn: int = 3
    repeat_halt: int = 5
    # One-shot early-stop nudge (D). If the agent stops (emits no tool calls)
    # before min_writes is met and iters remain, nudge ONCE to continue or to call
    # ask_human. Bounded; does NOT fire once min_writes is satisfied — Boundary is
    # bounded, not maximal, so we never push a satisfied run to "explore more".
    nudge_on_early_stop: bool = True
    # Staging pivot: agents get a small orientation window, then must commit to
    # a provisional thesis/hypothesis/evidence plan before deep reads or writes.
    require_staging: bool = True
    max_unstaged_reads: int = 3
    # Spend caps — None disables.
    max_input_tokens: int | None = 500_000
    max_output_tokens: int | None = 50_000
    max_dollars: float | None = None
    # Wall-clock safety net (None = disabled). Catches hung tools, network stalls.
    max_wall_seconds: float | None = 900.0  # 15 min default
    # Commit-tool policy. See BASH_COMMIT_DENYLIST comment above.
    #   "refuse"   — REFUSE any kind="commit" call. Default for headless. Bash
    #                commands matching the denylist are also refused with
    #                instructions to switch to bash_commit.
    #   "queue"    — halt the run and queue a review entry (mirrors ambiguity_halt).
    #                Resume after human approval via /boundary-review.
    #   "ask"      — interactive only: route to ask_human and let the agent
    #                consume the user's response as the tool result.
    #   "allow"    — execute. Use with commit_allowlist to restrict which
    #                commit tools are allowed.
    on_commit: str = "refuse"
    # Per-tool allowlist. Only checked when on_commit == "allow". Empty list
    # under "allow" means ALL commit tools are allowed (use with caution).
    commit_allowlist: list[str] = field(default_factory=list)
    # Taint policy (Item 3). A run becomes "tainted" when it handles untrusted
    # content: a fetch_url (external), a read_file/grep of a file the persisted
    # ledger marks tainted, or a bash call when egress is not OS-bounded (driver
    # != srt). A subsequent write/commit to a writable sink is then a potential
    # exfil channel. Taint is coarse and file-granular (which files, not which
    # bytes) and persists in a per-workspace ledger (see boundary/taint.py) so it
    # carries across pipeline stages and separate runs; a write done while tainted
    # marks its output file tainted too.
    #   "warn"   — record a taint_flow event but allow the write (default).
    #   "refuse" — block the write; tainted content must not reach a writable sink.
    #   "allow"  — disable the check (a downgrade; surfaced by the Third Umpire).
    on_taint: str = "warn"
    # USD per 1M tokens by model id. "cached" defaults to 0.1× input if absent.
    # Source: published rates as of 2026.
    token_rates: dict = field(default_factory=lambda: {
        "claude-sonnet-4.5":   {"input": 3.0,  "cached": 0.30, "output": 15.0},
        "claude-sonnet-4.6":   {"input": 3.0,  "cached": 0.30, "output": 15.0},
        "claude-opus-4.5":     {"input": 15.0, "cached": 1.50, "output": 75.0},
        "claude-opus-4.6":     {"input": 15.0, "cached": 1.50, "output": 75.0},
        "claude-opus-4.7":     {"input": 15.0, "cached": 1.50, "output": 75.0},
        "claude-haiku-4.5":    {"input": 0.80, "cached": 0.08, "output": 4.0},
        # OpenAI: cached input ~25% of full input rate
        "gpt-5.5":             {"input": 5.0,  "cached": 1.25, "output": 20.0},
        "gpt-5.4":             {"input": 2.5,  "cached": 0.625, "output": 10.0},
        "gpt-5.4-mini":        {"input": 0.50, "cached": 0.125, "output": 2.0},
        "gpt-4.1":             {"input": 2.0,  "cached": 0.50, "output": 8.0},
        "Qwen/Qwen2.5-Coder-32B-Instruct": {"input": 0.80, "cached": 0.80, "output": 0.80},
    })

    def estimate_cost(self, model: str, in_tok: int, out_tok: int, cached_tok: int = 0) -> float:
        r = self.token_rates.get(model)
        if not r:
            return 0.0
        cached_rate = r.get("cached", r["input"] * 0.1)
        fresh_in = max(in_tok - cached_tok, 0)
        return (
            (fresh_in / 1_000_000) * r["input"]
            + (cached_tok / 1_000_000) * cached_rate
            + (out_tok / 1_000_000) * r["output"]
        )

    def path_allowed(self, path: str) -> bool:
        if not self.writable_paths:
            return False
        candidates = [path, path.lstrip("/")]
        for pat in self.writable_paths:
            for c in candidates:
                if c == pat or fnmatch.fnmatch(c, pat):
                    return True
        return False


@dataclass
class EnvelopeEvent:
    kind: str  # "staged" | "staging_required" | "write_allowed" | "write_refused" | "missing_reason" | "ambiguity_halt" | "limit_hit" | "commit_refused" | "commit_allowed" | "commit_halt" | "bash_commit_blocked" | "taint_flow" | "taint_egress" | "no_progress" | "early_stop_nudge"
    tool: str
    detail: str
    iteration: int


@dataclass
class EnvelopeRunResult:
    loop_result: LoopResult
    events: list[EnvelopeEvent]
    writes_attempted: int
    writes_executed: int
    appends_executed: int
    external_calls: int
    halted_for_ambiguity: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    estimated_dollars: float = 0.0
    halted_for_budget: bool = False
    halted_for_wallclock: bool = False
    wall_seconds: float = 0.0
    commit_calls: int = 0
    commit_refused: int = 0
    halted_for_commit: bool = False
    staged: bool = False
    unstaged_reads: int = 0
    results_by_class: dict = field(default_factory=dict)


# Typed tool-result feedback (A). Classify each tool result into one of four
# categories so the agent self-corrects on a labeled signal and the Third Umpire
# / history / best-of-K judge see a run's execution-quality profile. ComPilot's
# RQ3 lens: typed feedback drove the agent's unproductive-proposal rate down
# across iterations vs an opaque string.
_ARG_INVALID_PREFIXES = (
    "ERROR: unknown tool",
    "ERROR: invalid call",
    "ERROR: file not found",
    "ERROR: not a regular file",
    "ERROR: not a directory",
    "ERROR: old_str not found",
    "ERROR: old_str matches",
)
_RUNTIME_PREFIXES = (
    "ERROR: command timed out",
    "ERROR: unknown sandbox driver",
)


def classify_tool_result(result: str, raised: Exception | None = None) -> str:
    """Return one of: success | arg-invalid | policy-refused | runtime-error.

    - arg-invalid:    malformed call or precondition violation (reform the call)
    - policy-refused: well-formed, but the envelope refused it (change approach / ask_human)
    - runtime-error:  the operation ran but failed at execution (address the condition)
    - success:        everything else
    """
    if raised is not None:
        return "arg-invalid" if isinstance(raised, (TypeError, KeyError, ValueError)) else "runtime-error"
    r = (result or "").lstrip()
    if r.startswith("ENVELOPE REFUSED") or r.startswith("[HALTED]"):
        return "policy-refused"
    if r.startswith(_ARG_INVALID_PREFIXES):
        return "arg-invalid"
    if r.startswith(_RUNTIME_PREFIXES):
        return "runtime-error"
    m = re.search(r"\[exit (-?\d+)\]", r[:200])
    if m:
        return "success" if m.group(1) == "0" else "runtime-error"
    if r.startswith("ERROR:"):
        return "runtime-error"
    return "success"


def _prevalidate_call(tool: Tool, arguments: dict) -> str | None:
    """Pre-exec validity gate (B). Reject a malformed call BEFORE executing the
    (possibly expensive/side-effecting) tool — ComPilot's cheap two-stage filter.

    v1: required-fields only. 'reason' is excluded — it is a policy concern
    enforced separately (require_reason → policy-refused), not arg-validity.
    Returns an arg-invalid message if the call is malformed, else None.
    """
    schema = tool.parameters or {}
    required = [f for f in (schema.get("required") or []) if f != "reason"]
    if not required:
        return None
    args = arguments or {}
    missing = [f for f in required if f not in args or args.get(f) is None]
    if missing:
        return (
            f"ERROR: invalid call to {tool.name}: missing required field(s) "
            f"{missing}. The tool was NOT executed — no cost incurred. Provide "
            f"{missing} and retry."
        )
    return None


def _make_enforced_tool(
    base: Tool,
    envelope: Envelope,
    counters: dict[str, int],
    events: list[EnvelopeEvent],
    iter_ref: list[int],
    halt_flag: list[bool] | None = None,
    commit_halt_flag: list[bool] | None = None,
    store=None,                         # TaintStore | None
    sandbox_driver: str = "seatbelt",
    egress_allowlist: list[str] | None = None,
) -> Tool:
    """Wrap a tool so it consults the envelope before executing."""
    original_fn = base.fn

    def enforced(**kwargs):
        i = iter_ref[0]
        staging_required = envelope.require_staging and bool(envelope.writable_paths)

        if staging_required and base.name == "read_file" and counters.get("staged", 0) == 0:
            counters["unstaged_reads"] = counters.get("unstaged_reads", 0) + 1
            if counters["unstaged_reads"] > envelope.max_unstaged_reads:
                events.append(EnvelopeEvent(
                    "staging_required", base.name,
                    f"unstaged_reads={counters['unstaged_reads']} max={envelope.max_unstaged_reads}", i,
                ))
                return (
                    "ENVELOPE REFUSED: orientation reads are exhausted. "
                    "Call `stage_proposal` with a tentative answer, hypotheses, "
                    "evidence plan, intended write, cost class, and kill criteria "
                    "before doing more deep reads. Anti-boil-the-ocean rule: every "
                    "next read must test or revise the staged answer."
                )

        if (
            staging_required
            and counters.get("staged", 0) == 0
            and (base.kind in ("write", "commit") or base.name == "bash")
        ):
            events.append(EnvelopeEvent("staging_required", base.name, "write_or_commit_before_stage", i))
            return (
                "ENVELOPE REFUSED: stage the candidate answer before writing or "
                "committing. Call `stage_proposal` first so write rejection resumes "
                "from staging instead of restarting research."
            )

        # Provenance taint (C): reading tainted workspace content taints the run.
        if store is not None:
            if base.name == "read_file" and store.is_tainted(kwargs.get("path", "")):
                counters["tainted_reads"] = counters.get("tainted_reads", 0) + 1
                counters.setdefault("tainted_sources", []).append("taint-file:" + str(kwargs.get("path", ""))[:80])
            elif base.name == "grep" and store.has_any():
                # Coarse on purpose: any grep over a workspace that holds a tainted
                # file taints the run, regardless of the grep's glob. We deliberately
                # do NOT try to intersect the glob with the tainted set — fnmatch's
                # glob semantics differ from pathlib's (e.g. "**/*" doesn't match a
                # top-level file under fnmatch), so a glob-overlap check would
                # under-taint (the unsafe direction). Over-approximation is the safe
                # choice for a security gate. read_file above is precise (exact path).
                counters["tainted_reads"] = counters.get("tainted_reads", 0) + 1
                counters.setdefault("tainted_sources", []).append("taint-grep:" + str(kwargs.get("glob", "**/*"))[:80])

        # 1. Reason check
        if envelope.require_reason and base.kind in ("write", "external", "commit"):
            reason = kwargs.get("reason", "").strip() if isinstance(kwargs.get("reason"), str) else ""
            if not reason:
                events.append(EnvelopeEvent("missing_reason", base.name, "(no reason)", i))
                return f"ENVELOPE REFUSED: tool '{base.name}' is a {base.kind} tool — you must include a non-empty 'reason' field."

        # Helper: did the underlying tool return a soft error sentinel?
        def _is_error_result(r) -> bool:
            return isinstance(r, str) and r.startswith("ERROR:")

        # 1b. Taint gate (Item 3) — tainted (untrusted external) content flowing
        #     into a writable sink. Coarse: once any tainted read has happened,
        #     every write/commit is a potential exfil channel.
        if (
            envelope.on_taint != "allow"
            and counters.get("tainted_reads", 0) > 0
            and (base.kind in ("write", "commit") or base.name == "bash")
        ):
            sources = counters.get("tainted_sources", [])  # type: ignore[assignment]
            events.append(EnvelopeEvent(
                "taint_flow", base.name,
                f"on_taint={envelope.on_taint} tainted_reads={counters['tainted_reads']} "
                f"sources={sources[:3]}", i,
            ))
            if envelope.on_taint == "refuse":
                return (
                    f"ENVELOPE REFUSED: this run read untrusted external content "
                    f"(taint sources: {sources[:3]}) and a write to a shared/writable path "
                    f"is a potential exfiltration channel (on_taint=refuse). Do not route "
                    f"untrusted content into a write. If this flow is intentional and safe, "
                    f"re-scope with on_taint=warn, or stage only trusted/derived content."
                )
            # warn: record the flow, let the write proceed.

        # 2. append_file — continuation of a prior write_file. Counted against
        #    max_appends, NOT max_writes. Lets the agent chunk a long write
        #    across multiple tool calls to bypass per-response output caps.
        if base.name == "append_file":
            path = kwargs.get("path", "")
            if not envelope.path_allowed(path):
                events.append(EnvelopeEvent("write_refused", base.name, f"path={path}", i))
                return (
                    f"ENVELOPE REFUSED: path '{path}' is not in writable_paths "
                    f"{envelope.writable_paths}."
                )
            if counters.get("appends_executed", 0) >= envelope.max_appends:
                events.append(EnvelopeEvent("limit_hit", base.name, f"max_appends={envelope.max_appends}", i))
                return f"ENVELOPE REFUSED: max_appends ({envelope.max_appends}) reached."
            kwargs_no_reason = {k: v for k, v in kwargs.items() if k != "reason"}
            try:
                result = original_fn(**kwargs_no_reason)
            except Exception as e:
                events.append(EnvelopeEvent("write_failed", base.name, f"{type(e).__name__}: {e}", i))
                raise
            if _is_error_result(result):
                events.append(EnvelopeEvent("write_failed", base.name, str(result)[:200], i))
                return result
            counters["appends_executed"] = counters.get("appends_executed", 0) + 1
            if store is not None and counters.get("tainted_reads", 0) > 0:
                store.mark_file(path)
            events.append(EnvelopeEvent("write_allowed", base.name, f"path={path} (append)", i))
            return result

        # 3. write_file / edit_file — count only on success. Failed attempts
        #    (exceptions or "ERROR:" sentinels) bump writes_attempted but NOT
        #    writes_executed, so a TypeError on missing kwargs doesn't burn the
        #    write budget.
        if base.kind == "write" and base.name in ("write_file", "edit_file"):
            counters["writes_attempted"] = counters.get("writes_attempted", 0) + 1
            path = kwargs.get("path", "")
            if not envelope.path_allowed(path):
                events.append(EnvelopeEvent("write_refused", base.name, f"path={path}", i))
                return (
                    f"ENVELOPE REFUSED: path '{path}' is not in writable_paths "
                    f"{envelope.writable_paths}. Either confirm with the user (call ask_human) "
                    f"or write only to allowed paths."
                )
            if counters.get("writes_executed", 0) >= envelope.max_writes:
                events.append(EnvelopeEvent("limit_hit", base.name, f"max_writes={envelope.max_writes}", i))
                return (
                    f"ENVELOPE REFUSED: max_writes ({envelope.max_writes}) reached. "
                    f"If you need to extend an existing write, use append_file (counted "
                    f"against max_appends={envelope.max_appends}, not max_writes)."
                )
            kwargs_no_reason = {k: v for k, v in kwargs.items() if k != "reason"}
            try:
                result = original_fn(**kwargs_no_reason)
            except Exception as e:
                events.append(EnvelopeEvent("write_failed", base.name, f"{type(e).__name__}: {e}", i))
                raise
            if _is_error_result(result):
                events.append(EnvelopeEvent("write_failed", base.name, str(result)[:200], i))
                return result
            counters["writes_executed"] = counters.get("writes_executed", 0) + 1
            if store is not None and counters.get("tainted_reads", 0) > 0:
                store.mark_file(path)
            events.append(EnvelopeEvent("write_allowed", base.name, f"path={path}", i))
            return result

        # 4. Bash special case — counts as a write iff envelope.allow_bash.
        #    Same success-only accounting as write_file/edit_file.
        if base.name == "bash":
            if not envelope.allow_bash:
                return "ENVELOPE REFUSED: bash is disabled for this run."
            # Egress denylist — see BASH_COMMIT_DENYLIST docstring at top of file.
            cmd_str = kwargs.get("command", "") if isinstance(kwargs.get("command"), str) else ""
            is_commit, matched = _bash_command_is_commit(cmd_str)
            if is_commit:
                events.append(EnvelopeEvent(
                    "bash_commit_blocked", base.name,
                    f"matched={matched} command={cmd_str[:120]}", i,
                ))
                return (
                    f"ENVELOPE REFUSED: bash command starts with '{matched}', "
                    f"which is on the commit-tool denylist (irreversible side effect). "
                    f"If this is intentional, call `bash_commit` with the same command "
                    f"and a 'reason' field. The run's commit policy will decide whether "
                    f"to allow, queue, ask, or refuse."
                )
            counters["writes_attempted"] = counters.get("writes_attempted", 0) + 1
            if counters.get("writes_executed", 0) >= envelope.max_writes:
                events.append(EnvelopeEvent("limit_hit", base.name, f"max_writes={envelope.max_writes}", i))
                return f"ENVELOPE REFUSED: max_writes ({envelope.max_writes}) reached."
            kwargs_no_reason = {k: v for k, v in kwargs.items() if k != "reason"}
            try:
                result = original_fn(**kwargs_no_reason)
            except Exception as e:
                events.append(EnvelopeEvent("write_failed", base.name, f"{type(e).__name__}: {e}", i))
                raise
            if _is_error_result(result):
                events.append(EnvelopeEvent("write_failed", base.name, str(result)[:200], i))
                return result
            counters["writes_executed"] = counters.get("writes_executed", 0) + 1
            # D: bash is a tainting SOURCE — it can fetch/exfil when egress is not
            # OS-bounded. Taint only after it actually executed (a refused bash
            # pulled nothing); subsequent write/commit/bash sinks then get gated.
            # Limitation (spec §5): we taint the RUN, not the specific files bash
            # wrote — we can't parse what a shell command wrote. So files created
            # by bash are not individually marked in the ledger and a later run
            # reading them won't be tainted by provenance. Use srt (egress bounded)
            # for bash you don't want treated as an untrusted source.
            if sandbox_driver != "srt":
                counters["tainted_reads"] = counters.get("tainted_reads", 0) + 1
                _bash_src = "bash:" + cmd_str[:60]
                counters.setdefault("tainted_sources", []).append(_bash_src)
                if store is not None:
                    store.mark_source(_bash_src)
            events.append(EnvelopeEvent("write_allowed", base.name, "bash", i))
            return result

        # 5. Commit-tool gate — irreversible external actions.
        if base.kind == "commit":
            counters["commit_attempted"] = counters.get("commit_attempted", 0) + 1
            policy = envelope.on_commit
            allowlist = envelope.commit_allowlist or []
            if policy == "allow" and (not allowlist or base.name in allowlist):
                # Execute. For bash_commit we apply the same success-only
                # accounting as a write so it counts against max_writes.
                if base.name == "bash_commit":
                    counters["writes_attempted"] = counters.get("writes_attempted", 0) + 1
                    if counters.get("writes_executed", 0) >= envelope.max_writes:
                        events.append(EnvelopeEvent("limit_hit", base.name, f"max_writes={envelope.max_writes}", i))
                        return f"ENVELOPE REFUSED: max_writes ({envelope.max_writes}) reached."
                kwargs_no_reason = {k: v for k, v in kwargs.items() if k != "reason"}
                try:
                    result = original_fn(**kwargs_no_reason)
                except Exception as e:
                    events.append(EnvelopeEvent("commit_refused", base.name, f"{type(e).__name__}: {e}", i))
                    raise
                if isinstance(result, str) and result.startswith("ERROR:"):
                    events.append(EnvelopeEvent("commit_refused", base.name, str(result)[:200], i))
                    return result
                counters["commit_executed"] = counters.get("commit_executed", 0) + 1
                if base.name == "bash_commit":
                    counters["writes_executed"] = counters.get("writes_executed", 0) + 1
                events.append(EnvelopeEvent("commit_allowed", base.name, f"policy=allow", i))
                return result
            # Not allowed by policy.
            if policy == "queue":
                if commit_halt_flag is not None:
                    commit_halt_flag[0] = True
                events.append(EnvelopeEvent(
                    "commit_halt", base.name,
                    f"queued for human approval — args={list(kwargs.keys())}", i,
                ))
                return (
                    f"[HALTED] Commit tool '{base.name}' requires human approval. "
                    f"The run will stop and be queued for review. "
                    f"Args snapshot: {kwargs}"
                )
            if policy == "ask":
                # Interactive: route through ask_human if available, otherwise
                # fall back to refuse with explicit instructions.
                if halt_flag is not None:
                    halt_flag[0] = True
                events.append(EnvelopeEvent(
                    "commit_halt", base.name,
                    f"ask_human required — args={list(kwargs.keys())}", i,
                ))
                return (
                    f"[HALTED] Commit tool '{base.name}' requires explicit human "
                    f"confirmation. Call `ask_human` describing the intended action "
                    f"and the exact args, then re-attempt only if the human approves."
                )
            # Default / "refuse" / unknown policy / "allow" but not in allowlist
            events.append(EnvelopeEvent(
                "commit_refused", base.name,
                f"policy={policy} allowlist={allowlist}", i,
            ))
            if policy == "allow" and allowlist and base.name not in allowlist:
                return (
                    f"ENVELOPE REFUSED: commit tool '{base.name}' is not in "
                    f"this run's commit_allowlist {allowlist}. "
                    f"Either re-scope the task to avoid this commit, or stop and "
                    f"surface the gap via ask_human."
                )
            return (
                f"ENVELOPE REFUSED: tool '{base.name}' is a commit tool "
                f"(irreversible external side effect) and this run's commit policy "
                f"is '{policy}'. Do not retry. If the task genuinely requires this "
                f"commit, stop and report via ask_human — a human must re-scope "
                f"the run with on_commit=allow + commit_allowlist=['{base.name}']."
            )

        # 6. External rate cap
        if base.kind == "external":
            counters["external_calls"] = counters.get("external_calls", 0) + 1
            if counters["external_calls"] > envelope.max_external:
                events.append(EnvelopeEvent("limit_hit", base.name, f"max_external={envelope.max_external}", i))
                return f"ENVELOPE REFUSED: max_external ({envelope.max_external}) reached."
            # Taint labeling (Item 3): external content is untrusted. Mark the run
            # tainted so a later write to a writable sink trips the taint gate.
            prior_taint = counters.get("tainted_reads", 0) > 0
            counters["tainted_reads"] = counters.get("tainted_reads", 0) + 1
            src = kwargs.get("url") or kwargs.get("query") or base.name
            counters.setdefault("tainted_sources", []).append(str(src)[:80])  # type: ignore[arg-type]
            if store is not None:
                store.mark_source(str(src)[:80])
            # nudge: already-tainted run fetching an off-allowlist host = possible exfil
            if prior_taint and base.name == "fetch_url":
                host = urlsplit(str(kwargs.get("url", ""))).hostname or ""
                if host and host not in (egress_allowlist or []):
                    events.append(EnvelopeEvent("taint_egress", base.name, f"host={host} off-allowlist", i))

        # 7. Default path — read tools and unmetered externals
        kwargs_no_reason = {k: v for k, v in kwargs.items() if k != "reason"}
        return original_fn(**kwargs_no_reason)

    return Tool(
        name=base.name,
        description=base.description,
        parameters=base.parameters,
        fn=enforced,
        kind=base.kind,
    )


def _ask_human_tool(halt_flag: list[bool], events: list[EnvelopeEvent], iter_ref: list[int]) -> Tool:
    def ask_human(question: str, options: list | None = None) -> str:
        halt_flag[0] = True
        events.append(EnvelopeEvent("ambiguity_halt", "ask_human", question[:200], iter_ref[0]))
        opts = ("\nOptions: " + json.dumps(options)) if options else ""
        return f"[HALTED] Agent requested human input: {question}{opts}\n(Loop will stop.)"
    return Tool(
        name="ask_human",
        description="Halt and surface an ambiguity to the human. Use when the task is underspecified or you would otherwise interpolate. Calling this STOPS the loop — only call when truly blocked.",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["question"],
        },
        fn=ask_human,
        kind="read",
    )


def _stage_proposal_tool(counters: dict[str, int], events: list[EnvelopeEvent], iter_ref: list[int]) -> Tool:
    def stage_proposal(
        thesis: str,
        hypotheses: list,
        evidence_plan: list,
        intended_write: str | None = None,
        cost_class: str = "unknown",
        kill_criteria: list | None = None,
    ) -> str:
        counters["staged"] = 1
        counters["stage_calls"] = counters.get("stage_calls", 0) + 1
        # Record the taint set that fed the thesis/evidence so far (Item 3).
        taint = counters.get("tainted_reads", 0)
        taint_note = f" taint={taint}({counters.get('tainted_sources', [])[:3]})" if taint else ""
        summary = (
            f"thesis={thesis[:120]} cost_class={cost_class} "
            f"intended_write={intended_write or '(none)'}{taint_note}"
        )
        events.append(EnvelopeEvent("staged", "stage_proposal", summary, iter_ref[0]))
        return (
            "STAGED: proposal accepted. Continue only with reads that test, kill, "
            "or strengthen this staged answer; write rejection will resume from "
            "this staging area instead of restarting research."
        )
    return Tool(
        name="stage_proposal",
        description=(
            "Stage the provisional answer before deep reading sprawls or before any write. "
            "Use after orientation reads: provide answer-first thesis, 2-3 hypotheses, "
            "smallest evidence plan, intended write path/action, cost class, and kill criteria. "
            "This is the anti-boil-the-ocean pivot."
        ),
        parameters={
            "type": "object",
            "properties": {
                "thesis": {"type": "string"},
                "hypotheses": {"type": "array", "items": {"type": "string"}},
                "evidence_plan": {"type": "array", "items": {"type": "string"}},
                "intended_write": {"type": "string"},
                "cost_class": {"type": "string"},
                "kill_criteria": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["thesis", "hypotheses", "evidence_plan"],
        },
        fn=stage_proposal,
        kind="read",
    )


ENVELOPE_NOTE_TEMPLATE = """

---

## ENVELOPE (enforced at the tool layer — not just suggestions)

You are operating inside an envelope. The runtime will REFUSE tool calls or halt
the loop if you exceed it.

- **Writable paths (workspace-relative):** {writable_paths}
  Any write outside this list will be refused. Do not retry — call `ask_human`.
- **Max writes:** {max_writes}. **Min writes:** {min_writes}. **Max appends:** {max_appends}.
  Plan to produce your first write by ~60% of iteration budget. Read, write a
  first draft, then iterate via `edit_file`. Do NOT save the write to the end.
- **Chunking long writes:** the per-response output cap is ~16k tokens, and
  extended thinking eats into that. If you expect to emit more than ~8k tokens
  of new content into a single file, split it: call `write_file` for the first
  chunk, then `append_file` for each subsequent chunk. `append_file` does NOT
  count against `max_writes` (it counts against `max_appends={max_appends}`).
  Treat the full chunked sequence as one logical write.
- **Iteration budget:** {max_iters}. You will get a [envelope] nudge at 60% and
  80% if you haven't written yet — treat those as hard signals to write now.
- **Staging pivot:** after {max_unstaged_reads} orientation `read_file` calls,
  the runtime will refuse more deep reads until you call `stage_proposal`.
  Stage an answer-first thesis, 2-3 hypotheses, smallest evidence plan,
  intended write/action, cost class, and kill criteria. Reads stay free with
  respect to the write budget, but they are not directionless: every read after
  staging must test, kill, or strengthen the staged answer. Do not boil the ocean.
- **Write gate resumes from staging:** if a write/commit is refused, revise from
  the same staged proposal and reads. Do not restart the read phase.
- **Spend budget:** the runtime tracks input/output tokens and halts when the
  cap is hit. Spend is the real budget; iterations are a safety net. Each
  pressure nudge tells you tokens used — if you're burning tokens reading the
  same files repeatedly, stop and write.
- **Revise with diffs, not rewrites.** When changing a file you already wrote,
  emit the smallest `edit_file` diff — do NOT re-emit the whole file. Output
  tokens are your most expensive budget (~5× input rate); re-dumping a file to
  change a paragraph is the single biggest avoidable cost.
- **Act on observed tool results, not assumptions.** Your grounded signal is the
  tool output in this loop, not context you were primed with. If a read or
  command result contradicts your plan, follow the result. Do not pad reasoning
  with unverified context — verify it with a tool or label it as unverified.
- **Failed writes do not consume your write budget.** If a `write_file` /
  `edit_file` call raises (e.g. TypeError on missing args) or returns an
  `ERROR:` sentinel, the attempt is logged but `writes_executed` does NOT
  increment. Read the error, fix the call, and retry.
- **Reason required:** every `write`/`external` tool call needs a non-empty `reason` field.
- **Ambiguity:** if the task is underspecified OR you would otherwise interpolate,
  call `ask_human(question=..., options=[...])`. This halts the loop cleanly.
  Stopping is correct behavior. Interpolating is the failure mode.
- **Source hierarchy:** wiki first, repo second, general search third. Cite the
  tier that produced each finding.
- **Grounding:** every quantitative claim in your final message must trace to a
  specific tool result in this session. If you cannot cite, do not state.
- **Final message:** concise summary. The user sees `--- final ---` plus your
  last assistant turn — no transcript replay.
"""


class EnvelopeRunner:
    def __init__(self, agent: Agent, envelope: Envelope):
        self.agent = agent
        self.envelope = envelope

    def _enforced_registry(self, halt_flag, events, iter_ref, commit_halt_flag) -> ToolRegistry:
        new_reg = ToolRegistry()
        counters: dict[str, int] = {}
        store = TaintStore.load(self.agent.workspace.root)
        for name, tool in self.agent.tools._tools.items():
            new_reg.register(_make_enforced_tool(
                tool, self.envelope, counters, events, iter_ref,
                halt_flag=halt_flag, commit_halt_flag=commit_halt_flag,
                store=store,
                sandbox_driver=self.agent.sandbox_driver,
                egress_allowlist=self.agent.egress_allowlist,
            ))
        if self.envelope.stop_on_ambiguity:
            new_reg.register(_ask_human_tool(halt_flag, events, iter_ref))
        if self.envelope.require_staging and self.envelope.writable_paths:
            new_reg.register(_stage_proposal_tool(counters, events, iter_ref))
        # expose counters for the runner
        new_reg._counters = counters  # type: ignore[attr-defined]
        return new_reg

    def run(self, task: str, verbose: bool = False, **chat_kwargs) -> EnvelopeRunResult:
        halt_flag = [False]
        commit_halt_flag = [False]
        events: list[EnvelopeEvent] = []
        iter_ref = [0]
        enforced = self._enforced_registry(halt_flag, events, iter_ref, commit_halt_flag)
        envelope_note = ENVELOPE_NOTE_TEMPLATE.format(
            writable_paths=self.envelope.writable_paths,
            max_writes=self.envelope.max_writes,
            min_writes=self.envelope.min_writes,
            max_appends=self.envelope.max_appends,
            max_iters=self.agent.max_iters,
            max_unstaged_reads=self.envelope.max_unstaged_reads,
        )
        system = self.agent.system_prompt + envelope_note
        messages = [
            Message(role="system", content=system),
            Message(role="user", content=task),
        ]
        if self.agent.transcript:
            self.agent.transcript.log("envelope_start",
                writable_paths=self.envelope.writable_paths,
                max_writes=self.envelope.max_writes,
                max_appends=self.envelope.max_appends,
                max_input_tokens=self.envelope.max_input_tokens,
                max_output_tokens=self.envelope.max_output_tokens,
                max_dollars=self.envelope.max_dollars,
                require_staging=self.envelope.require_staging,
                max_unstaged_reads=self.envelope.max_unstaged_reads,
                task=task,
            )

        # Run loop with halt hook + budget-pressure injections + spend/wall caps
        import time as _time
        import json as _json
        from boundary.clients.base import ChatResponse
        tool_schemas = enforced.schemas()
        max_iters = self.agent.max_iters
        model_name = getattr(self.agent.client, "model", "unknown")
        pressure_iters = sorted({int(max_iters * f) for f in self.envelope.budget_pressure_at if 0 < f < 1})
        pressure_fired: set[int] = set()
        action_counts: dict[str, int] = {}
        results_by_class: dict[str, int] = {}
        no_progress_halt = False
        early_stop_nudged = False
        total_in = 0
        total_out = 0
        total_cached = 0
        halted_for_budget = False
        halted_for_wallclock = False
        wall_start = _time.time()
        for i in range(1, max_iters + 1):
            iter_ref[0] = i

            # Wall-clock safety net
            if self.envelope.max_wall_seconds is not None:
                elapsed = _time.time() - wall_start
                if elapsed >= self.envelope.max_wall_seconds:
                    halted_for_wallclock = True
                    events.append(EnvelopeEvent(
                        "wallclock_halt", "loop",
                        f"elapsed={elapsed:.1f}s cap={self.envelope.max_wall_seconds}s", i,
                    ))
                    if self.agent.transcript:
                        self.agent.transcript.log("wallclock_halt", iteration=i, elapsed_seconds=elapsed)
                    if verbose:
                        print(f"[{i}] ENVELOPE HALT: wall-clock cap reached ({elapsed:.1f}s)")
                    break

            # Spend gate
            est_dollars = self.envelope.estimate_cost(model_name, total_in, total_out, total_cached)
            over_in = self.envelope.max_input_tokens is not None and total_in >= self.envelope.max_input_tokens
            over_out = self.envelope.max_output_tokens is not None and total_out >= self.envelope.max_output_tokens
            over_dollars = self.envelope.max_dollars is not None and est_dollars >= self.envelope.max_dollars
            if over_in or over_out or over_dollars:
                halted_for_budget = True
                events.append(EnvelopeEvent(
                    "budget_halt", "model",
                    f"in={total_in} out={total_out} est=${est_dollars:.4f}", i,
                ))
                if self.agent.transcript:
                    self.agent.transcript.log("budget_halt",
                        iteration=i, input_tokens=total_in, output_tokens=total_out,
                        cached_input_tokens=total_cached, estimated_dollars=est_dollars,
                    )
                if verbose:
                    print(f"[{i}] ENVELOPE HALT: spend cap reached (in={total_in} out={total_out} ${est_dollars:.4f})")
                break

            # Budget-pressure nudge
            for pi in pressure_iters:
                if i == pi and pi not in pressure_fired:
                    pressure_fired.add(pi)
                    writes_so_far = enforced._counters.get("writes_executed", 0)  # type: ignore[attr-defined]
                    pct = int(100 * i / max_iters)
                    if writes_so_far < self.envelope.min_writes:
                        nudge = (
                            f"[envelope] iter {i}/{max_iters} ({pct}%). "
                            f"writes={writes_so_far} tokens_in={total_in} tokens_out={total_out}. "
                            f"You need {self.envelope.min_writes} write(s) to "
                            f"{self.envelope.writable_paths} before max_iters. "
                            f"Stop gathering and write now."
                        )
                        messages.append(Message(role="user", content=nudge))
                        if self.agent.transcript:
                            self.agent.transcript.log("budget_pressure",
                                iteration=i, writes_so_far=writes_so_far,
                                input_tokens=total_in, output_tokens=total_out, nudge=nudge,
                            )
                        if verbose:
                            print(f"[{i}] {nudge}")

            if self.agent.transcript:
                self.agent.transcript.log("request", iteration=i, n_messages=len(messages))
            chat_kwargs.setdefault("max_tokens", 32000)
            resp: ChatResponse = self.agent.client.chat(messages, tools=tool_schemas, **chat_kwargs)
            total_in += resp.input_tokens
            total_out += resp.output_tokens
            total_cached += resp.cached_input_tokens
            msg = resp.message
            messages.append(msg)
            if self.agent.transcript:
                self.agent.transcript.log("assistant",
                    iteration=i, content=msg.content,
                    tool_calls=[{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls],
                    finish_reason=resp.finish_reason,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    cached_input_tokens=resp.cached_input_tokens,
                    cumulative_in=total_in,
                    cumulative_out=total_out,
                    cumulative_cached=total_cached,
                )
            if verbose:
                if msg.content:
                    cache_note = f" cached={resp.cached_input_tokens}" if resp.cached_input_tokens else ""
                    print(f"[{i}] assistant ({resp.input_tokens}/{resp.output_tokens} tok{cache_note}): {msg.content[:400]}")
                for tc in msg.tool_calls:
                    print(f"[{i}] tool_call: {tc.name}({list(tc.arguments.keys())})")
            if not msg.tool_calls:
                if resp.finish_reason == "tool_calls" and i < max_iters:
                    messages.append(Message(role="user", content="(continue — you said you'd use tools; issue them now)"))
                    continue
                # D: one-shot early-stop nudge. The agent terminated; if it
                # under-delivered (writes < min_writes) and budget remains, nudge
                # once to finish or to call ask_human. Fires at most once.
                _writes_so_far = enforced._counters.get("writes_executed", 0)  # type: ignore[attr-defined]
                if (self.envelope.nudge_on_early_stop and not early_stop_nudged
                        and _writes_so_far < self.envelope.min_writes and i < max_iters):
                    early_stop_nudged = True
                    _iters_left = max_iters - i
                    _nudge = (
                        f"[envelope] you stopped at iter {i}/{max_iters} with "
                        f"{_writes_so_far}/{self.envelope.min_writes} required write(s) and "
                        f"{_iters_left} iters left. Either write to "
                        f"{self.envelope.writable_paths} now, or call ask_human if you are "
                        f"blocked. Do not stop under-delivered."
                    )
                    messages.append(Message(role="user", content=_nudge))
                    events.append(EnvelopeEvent("early_stop_nudge", "loop",
                        f"writes={_writes_so_far}/{self.envelope.min_writes}", i))
                    if self.agent.transcript:
                        self.agent.transcript.log("early_stop_nudge",
                            iteration=i, writes_so_far=_writes_so_far, nudge=_nudge)
                    if verbose:
                        print(f"[{i}] {_nudge}")
                    continue
                break
            for tc in msg.tool_calls:
                tool = enforced.get(tc.name)
                _raised: Exception | None = None
                if tool is None:
                    result = f"ERROR: unknown tool {tc.name}"
                else:
                    _preerr = _prevalidate_call(tool, tc.arguments)
                    if _preerr is not None:
                        # B: pre-exec validity gate — malformed call rejected
                        # before the (expensive/side-effecting) tool runs.
                        result = _preerr
                    else:
                        try:
                            result = tool.call(tc.arguments)
                        except Exception as e:
                            _raised = e
                            result = f"ERROR: {type(e).__name__}: {e}"
                # A: typed feedback classification — label every result so the
                # agent self-corrects on a category, not an opaque string.
                result_class = classify_tool_result(result, _raised)
                results_by_class[result_class] = results_by_class.get(result_class, 0) + 1
                # D: repeated-action / no-progress detection. Identical tool calls
                # (name + canonical args) repeated past a threshold signal a stuck
                # agent burning budget on unproductive exchanges. Warn in-band,
                # then halt the run once repeat_halt is reached.
                if self.envelope.repeat_halt:
                    try:
                        _sig = f"{tc.name}:{_json.dumps(tc.arguments, sort_keys=True, default=str)}"
                    except Exception:
                        _sig = f"{tc.name}:{tc.arguments!r}"
                    _rc = action_counts.get(_sig, 0) + 1
                    action_counts[_sig] = _rc
                    if _rc >= self.envelope.repeat_halt:
                        no_progress_halt = True
                        events.append(EnvelopeEvent("no_progress", tc.name, f"identical call x{_rc}", i))
                        result += (
                            f"\n[envelope] NO-PROGRESS HALT: this exact call has run "
                            f"{_rc}× with no new outcome. Stopping the run."
                        )
                    elif self.envelope.repeat_warn and _rc >= self.envelope.repeat_warn:
                        result += (
                            f"\n[envelope] repeated action: this exact call has run {_rc}× "
                            f"with no new outcome. Change approach or stop — do not repeat it."
                        )
                # Always-on budget banner: prefix every tool_result so the agent
                # cannot read a result without seeing remaining budget. This is
                # the "make constraints unavoidable, not buried in setup" fix.
                writes_used = enforced._counters.get("writes_executed", 0)  # type: ignore[attr-defined]
                appends_used = enforced._counters.get("appends_executed", 0)  # type: ignore[attr-defined]
                ext_used = enforced._counters.get("external_calls", 0)  # type: ignore[attr-defined]
                staged = enforced._counters.get("staged", 0)  # type: ignore[attr-defined]
                unstaged_reads = enforced._counters.get("unstaged_reads", 0)  # type: ignore[attr-defined]
                iters_left = max_iters - i
                est_now = self.envelope.estimate_cost(model_name, total_in, total_out, total_cached)
                banner_bits = [
                    f"writes {writes_used}/{self.envelope.max_writes}",
                    f"iters_left {iters_left}/{max_iters}",
                    f"tokens {total_in:,}in/{total_out:,}out",
                    f"${est_now:.4f}",
                    f"staged {'yes' if staged else 'no'}",
                    f"result {result_class}",
                ]
                if not staged and unstaged_reads:
                    banner_bits.append(f"orientation_reads {unstaged_reads}/{self.envelope.max_unstaged_reads}")
                if appends_used:
                    banner_bits.append(f"appends {appends_used}/{self.envelope.max_appends}")
                if self.envelope.max_dollars is not None:
                    banner_bits.append(f"cap ${self.envelope.max_dollars:.2f}")
                if ext_used:
                    banner_bits.append(f"ext {ext_used}/{self.envelope.max_external}")
                banner = "[ENVELOPE: " + " | ".join(banner_bits) + "]"
                wrapped_result = banner + "\n" + result
                if self.agent.transcript:
                    self.agent.transcript.log("tool_result", iteration=i, tool=tc.name, tool_call_id=tc.id, result=result[:2000], result_class=result_class)
                if verbose:
                    print(f"[{i}] tool_result {tc.name}: {result[:300]}")
                messages.append(Message(role="tool", content=wrapped_result, tool_call_id=tc.id, name=tc.name))
            if halt_flag[0] or commit_halt_flag[0] or no_progress_halt:
                break

        c = enforced._counters  # type: ignore[attr-defined]
        est = self.envelope.estimate_cost(model_name, total_in, total_out, total_cached)
        wall_seconds = _time.time() - wall_start
        if halted_for_wallclock:
            stop_reason = "wallclock_halt"
        elif halted_for_budget:
            stop_reason = "budget_halt"
        elif commit_halt_flag[0]:
            stop_reason = "commit_halt"
        elif halt_flag[0]:
            stop_reason = "ambiguity_halt"
        elif no_progress_halt:
            stop_reason = "no_progress_halt"
        else:
            stop_reason = "stop"
        loop_result = LoopResult(
            final_message=messages[-1] if messages else Message(role="assistant"),
            iterations=iter_ref[0],
            stop_reason=stop_reason,
            messages=messages,
        )
        if self.agent.transcript:
            self.agent.transcript.log("envelope_end",
                writes_attempted=c.get("writes_attempted", 0),
                writes_executed=c.get("writes_executed", 0),
                appends_executed=c.get("appends_executed", 0),
                external_calls=c.get("external_calls", 0),
                commit_attempted=c.get("commit_attempted", 0),
                commit_executed=c.get("commit_executed", 0),
                halted_for_ambiguity=halt_flag[0],
                halted_for_commit=commit_halt_flag[0],
                halted_for_budget=halted_for_budget,
                halted_for_wallclock=halted_for_wallclock,
                input_tokens=total_in,
                output_tokens=total_out,
                cached_input_tokens=total_cached,
                estimated_dollars=est,
                wall_seconds=wall_seconds,
                model=model_name,
                on_commit=self.envelope.on_commit,
                commit_allowlist=list(self.envelope.commit_allowlist or []),
                on_taint=self.envelope.on_taint,
                sandbox_driver=self.agent.sandbox_driver,
                egress_allowlist=list(self.agent.egress_allowlist or []),
                tainted_reads=c.get("tainted_reads", 0),
                staged=bool(c.get("staged", 0)),
                unstaged_reads=c.get("unstaged_reads", 0),
                stage_calls=c.get("stage_calls", 0),
                results_by_class=dict(results_by_class),
                events=[{"kind": e.kind, "tool": e.tool, "detail": e.detail, "iteration": e.iteration} for e in events],
            )
        return EnvelopeRunResult(
            loop_result=loop_result,
            events=events,
            writes_attempted=c.get("writes_attempted", 0),
            writes_executed=c.get("writes_executed", 0),
            appends_executed=c.get("appends_executed", 0),
            external_calls=c.get("external_calls", 0),
            halted_for_ambiguity=halt_flag[0],
            input_tokens=total_in,
            output_tokens=total_out,
            cached_input_tokens=total_cached,
            estimated_dollars=est,
            halted_for_budget=halted_for_budget,
            halted_for_wallclock=halted_for_wallclock,
            wall_seconds=wall_seconds,
            commit_calls=c.get("commit_executed", 0),
            commit_refused=c.get("commit_attempted", 0) - c.get("commit_executed", 0),
            halted_for_commit=commit_halt_flag[0],
            staged=bool(c.get("staged", 0)),
            unstaged_reads=c.get("unstaged_reads", 0),
            results_by_class=dict(results_by_class),
        )
