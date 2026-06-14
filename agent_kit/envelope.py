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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_kit.agent import Agent
from agent_kit.clients.base import Message
from agent_kit.loop import LoopResult, run_loop
from agent_kit.tools.registry import Tool, ToolRegistry


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
    # Spend caps — None disables.
    max_input_tokens: int | None = 500_000
    max_output_tokens: int | None = 50_000
    max_dollars: float | None = None
    # Wall-clock safety net (None = disabled). Catches hung tools, network stalls.
    max_wall_seconds: float | None = 900.0  # 15 min default
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
    kind: str  # "write_allowed" | "write_refused" | "missing_reason" | "ambiguity_halt" | "limit_hit"
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


def _make_enforced_tool(
    base: Tool,
    envelope: Envelope,
    counters: dict[str, int],
    events: list[EnvelopeEvent],
    iter_ref: list[int],
) -> Tool:
    """Wrap a tool so it consults the envelope before executing."""
    original_fn = base.fn

    def enforced(**kwargs):
        i = iter_ref[0]
        # 1. Reason check
        if envelope.require_reason and base.kind in ("write", "external"):
            reason = kwargs.get("reason", "").strip() if isinstance(kwargs.get("reason"), str) else ""
            if not reason:
                events.append(EnvelopeEvent("missing_reason", base.name, "(no reason)", i))
                return f"ENVELOPE REFUSED: tool '{base.name}' is a {base.kind} tool — you must include a non-empty 'reason' field."

        # Helper: did the underlying tool return a soft error sentinel?
        def _is_error_result(r) -> bool:
            return isinstance(r, str) and r.startswith("ERROR:")

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
            events.append(EnvelopeEvent("write_allowed", base.name, f"path={path}", i))
            return result

        # 4. Bash special case — counts as a write iff envelope.allow_bash.
        #    Same success-only accounting as write_file/edit_file.
        if base.name == "bash":
            if not envelope.allow_bash:
                return "ENVELOPE REFUSED: bash is disabled for this run."
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
            events.append(EnvelopeEvent("write_allowed", base.name, "bash", i))
            return result

        # 5. External rate cap
        if base.kind == "external":
            counters["external_calls"] = counters.get("external_calls", 0) + 1
            if counters["external_calls"] > envelope.max_external:
                events.append(EnvelopeEvent("limit_hit", base.name, f"max_external={envelope.max_external}", i))
                return f"ENVELOPE REFUSED: max_external ({envelope.max_external}) reached."

        # 6. Default path — read tools and unmetered externals
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
- **Spend budget:** the runtime tracks input/output tokens and halts when the
  cap is hit. Spend is the real budget; iterations are a safety net. Each
  pressure nudge tells you tokens used — if you're burning tokens reading the
  same files repeatedly, stop and write.
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

    def _enforced_registry(self, halt_flag, events, iter_ref) -> ToolRegistry:
        new_reg = ToolRegistry()
        counters: dict[str, int] = {}
        for name, tool in self.agent.tools._tools.items():
            new_reg.register(_make_enforced_tool(tool, self.envelope, counters, events, iter_ref))
        if self.envelope.stop_on_ambiguity:
            new_reg.register(_ask_human_tool(halt_flag, events, iter_ref))
        # expose counters for the runner
        new_reg._counters = counters  # type: ignore[attr-defined]
        return new_reg

    def run(self, task: str, verbose: bool = False, **chat_kwargs) -> EnvelopeRunResult:
        halt_flag = [False]
        events: list[EnvelopeEvent] = []
        iter_ref = [0]
        enforced = self._enforced_registry(halt_flag, events, iter_ref)
        envelope_note = ENVELOPE_NOTE_TEMPLATE.format(
            writable_paths=self.envelope.writable_paths,
            max_writes=self.envelope.max_writes,
            min_writes=self.envelope.min_writes,
            max_appends=self.envelope.max_appends,
            max_iters=self.agent.max_iters,
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
                task=task,
            )

        # Run loop with halt hook + budget-pressure injections + spend/wall caps
        import time as _time
        from agent_kit.clients.base import ChatResponse
        tool_schemas = enforced.schemas()
        max_iters = self.agent.max_iters
        model_name = getattr(self.agent.client, "model", "unknown")
        pressure_iters = sorted({int(max_iters * f) for f in self.envelope.budget_pressure_at if 0 < f < 1})
        pressure_fired: set[int] = set()
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
                break
            for tc in msg.tool_calls:
                tool = enforced.get(tc.name)
                if tool is None:
                    result = f"ERROR: unknown tool {tc.name}"
                else:
                    try:
                        result = tool.call(tc.arguments)
                    except Exception as e:
                        result = f"ERROR: {type(e).__name__}: {e}"
                # Always-on budget banner: prefix every tool_result so the agent
                # cannot read a result without seeing remaining budget. This is
                # the "make constraints unavoidable, not buried in setup" fix
                # from the Uatu/Fury jugalbandi.
                writes_used = enforced._counters.get("writes_executed", 0)  # type: ignore[attr-defined]
                appends_used = enforced._counters.get("appends_executed", 0)  # type: ignore[attr-defined]
                ext_used = enforced._counters.get("external_calls", 0)  # type: ignore[attr-defined]
                iters_left = max_iters - i
                est_now = self.envelope.estimate_cost(model_name, total_in, total_out, total_cached)
                banner_bits = [
                    f"writes {writes_used}/{self.envelope.max_writes}",
                    f"iters_left {iters_left}/{max_iters}",
                    f"tokens {total_in:,}in/{total_out:,}out",
                    f"${est_now:.4f}",
                ]
                if appends_used:
                    banner_bits.append(f"appends {appends_used}/{self.envelope.max_appends}")
                if self.envelope.max_dollars is not None:
                    banner_bits.append(f"cap ${self.envelope.max_dollars:.2f}")
                if ext_used:
                    banner_bits.append(f"ext {ext_used}/{self.envelope.max_external}")
                banner = "[ENVELOPE: " + " | ".join(banner_bits) + "]"
                wrapped_result = banner + "\n" + result
                if self.agent.transcript:
                    self.agent.transcript.log("tool_result", iteration=i, tool=tc.name, tool_call_id=tc.id, result=result[:2000])
                if verbose:
                    print(f"[{i}] tool_result {tc.name}: {result[:300]}")
                messages.append(Message(role="tool", content=wrapped_result, tool_call_id=tc.id, name=tc.name))
            if halt_flag[0]:
                break

        c = enforced._counters  # type: ignore[attr-defined]
        est = self.envelope.estimate_cost(model_name, total_in, total_out, total_cached)
        wall_seconds = _time.time() - wall_start
        if halted_for_wallclock:
            stop_reason = "wallclock_halt"
        elif halted_for_budget:
            stop_reason = "budget_halt"
        elif halt_flag[0]:
            stop_reason = "ambiguity_halt"
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
                halted_for_ambiguity=halt_flag[0],
                halted_for_budget=halted_for_budget,
                halted_for_wallclock=halted_for_wallclock,
                input_tokens=total_in,
                output_tokens=total_out,
                cached_input_tokens=total_cached,
                estimated_dollars=est,
                wall_seconds=wall_seconds,
                model=model_name,
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
        )
