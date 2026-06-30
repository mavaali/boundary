from __future__ import annotations

from dataclasses import dataclass

from boundary.clients.base import ChatResponse, Message, ModelClient
from boundary.tools.registry import ToolRegistry
from boundary.transcript import Transcript


@dataclass
class LoopResult:
    final_message: Message
    iterations: int
    stop_reason: str
    messages: list[Message]


def run_loop(
    client: ModelClient,
    messages: list[Message],
    tools: ToolRegistry,
    max_iters: int = 25,
    transcript: Transcript | None = None,
    verbose: bool = False,
    **chat_kwargs,
) -> LoopResult:
    tool_schemas = tools.schemas() if len(tools) else None
    for i in range(1, max_iters + 1):
        if transcript:
            transcript.log("request", iteration=i, n_messages=len(messages))
        resp: ChatResponse = client.chat(messages, tools=tool_schemas, **chat_kwargs)
        msg = resp.message
        messages.append(msg)
        if transcript:
            transcript.log(
                "assistant",
                iteration=i,
                content=msg.content,
                tool_calls=[{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls],
                finish_reason=resp.finish_reason,
            )
        if verbose:
            if msg.content:
                print(f"[{i}] assistant: {msg.content[:500]}")
            for tc in msg.tool_calls:
                print(f"[{i}] tool_call: {tc.name}({tc.arguments})")
        if not msg.tool_calls:
            # Some backends occasionally return finish_reason="tool_calls" with an
            # empty array (preamble-only chunk). Nudge once and continue instead of
            # bailing out.
            if resp.finish_reason == "tool_calls" and i < max_iters:
                messages.append(Message(
                    role="user",
                    content="(continue — you said you'd use tools; please issue the tool calls now)",
                ))
                continue
            return LoopResult(msg, i, resp.finish_reason, messages)

        for tc in msg.tool_calls:
            tool = tools.get(tc.name)
            if tool is None:
                result = f"ERROR: unknown tool {tc.name}"
            else:
                try:
                    result = tool.call(tc.arguments)
                except Exception as e:
                    result = f"ERROR: {type(e).__name__}: {e}"
            if transcript:
                transcript.log("tool_result", iteration=i, tool=tc.name, tool_call_id=tc.id, result=result[:2000])
            if verbose:
                print(f"[{i}] tool_result {tc.name}: {result[:300]}")
            messages.append(Message(
                role="tool",
                content=result,
                tool_call_id=tc.id,
                name=tc.name,
            ))
    return LoopResult(messages[-1], max_iters, "max_iters", messages)
