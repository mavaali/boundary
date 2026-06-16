from __future__ import annotations
import json
import os
from typing import Any

import httpx

from boundary.clients.base import ChatResponse, Message, ModelClient, ToolCall

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"


class AnthropicClient(ModelClient):
    """Anthropic Messages API. Translates OpenAI-style tool calls <-> Anthropic format."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        api_key: str | None = None,
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self.max_tokens = max_tokens
        self.timeout = timeout

    @staticmethod
    def _to_anthropic_messages(messages: list[Message]) -> tuple[str | None, list[dict]]:
        system = None
        out: list[dict] = []
        for m in messages:
            if m.role == "system":
                system = m.content
                continue
            if m.role == "tool":
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": m.content or "",
                    }],
                })
                continue
            if m.role == "assistant" and m.tool_calls:
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                out.append({"role": "assistant", "content": blocks})
                continue
            out.append({"role": m.role, "content": m.content or ""})
        return system, out

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        system, msgs = self._to_anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters", {"type": "object"}),
                }
                for t in tools
            ]
        for k in ("temperature", "top_p"):
            if k in kwargs:
                payload[k] = kwargs[k]
        r = httpx.post(
            ANTHROPIC_API,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"anthropic api {r.status_code}: {r.text[:500]}")
        data = r.json()
        text_parts = []
        tool_calls = []
        for block in data.get("content", []):
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append(ToolCall(
                    id=block["id"], name=block["name"], arguments=block.get("input", {})
                ))
        stop_reason = data.get("stop_reason", "end_turn")
        finish = "tool_calls" if stop_reason == "tool_use" else "stop"
        usage = data.get("usage") or {}
        # Anthropic puts cache hits in cache_read_input_tokens (separate from input_tokens).
        # input_tokens excludes cache reads; we add them back, then mark them cached.
        cache_read = int(usage.get("cache_read_input_tokens", 0))
        cache_create = int(usage.get("cache_creation_input_tokens", 0))
        base_in = int(usage.get("input_tokens", 0))
        return ChatResponse(
            message=Message(
                role="assistant",
                content="\n".join(text_parts) if text_parts else None,
                tool_calls=tool_calls,
            ),
            finish_reason=finish,
            raw=data,
            input_tokens=base_in + cache_read + cache_create,
            output_tokens=int(usage.get("output_tokens", 0)),
            cached_input_tokens=cache_read,
        )
