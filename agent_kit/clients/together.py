from __future__ import annotations
import json
import os
from typing import Any

import httpx

from agent_kit.clients.base import ChatResponse, Message, ModelClient, ToolCall

TOGETHER_API = "https://api.together.xyz/v1/chat/completions"


class TogetherClient(ModelClient):
    def __init__(
        self,
        model: str = "Qwen/Qwen2.5-Coder-32B-Instruct",
        api_key: str | None = None,
        timeout: float = 120.0,
    ):
        self.model = model
        self.api_key = api_key or os.environ.get("TOGETHER_API_KEY")
        if not self.api_key:
            raise RuntimeError("TOGETHER_API_KEY not set")
        self.timeout = timeout

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        for k in ("temperature", "max_tokens", "top_p"):
            if k in kwargs:
                payload[k] = kwargs[k]
        r = httpx.post(
            TOGETHER_API,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"together api {r.status_code}: {r.text[:500]}")
        data = r.json()
        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            args = tc["function"]["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    args = {"_raw": args}
            tool_calls.append(
                ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args)
            )
        return ChatResponse(
            message=Message(
                role="assistant",
                content=msg.get("content"),
                tool_calls=tool_calls,
            ),
            finish_reason=choice.get("finish_reason", "stop"),
            raw=data,
            input_tokens=int((data.get("usage") or {}).get("prompt_tokens", 0)),
            output_tokens=int((data.get("usage") or {}).get("completion_tokens", 0)),
        )
