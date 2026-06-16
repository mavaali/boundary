from __future__ import annotations
import json
import os
import time
from typing import Any

import httpx

from boundary.clients.base import ChatResponse, Message, ModelClient, ToolCall

OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient(ModelClient):
    """OpenAI-compatible client for OpenRouter (https://openrouter.ai).

    Model slugs are namespaced, e.g. "anthropic/claude-haiku-4.5".
    """

    def __init__(
        self,
        model: str = "anthropic/claude-haiku-4.5",
        api_key: str | None = None,
        timeout: float = 120.0,
    ):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
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
        # Retry once on transient provider errors — OpenRouter routes through
        # multiple providers and an individual one can 400 then succeed on retry.
        last_err = None
        for attempt in range(2):
            r = httpx.post(
                OPENROUTER_API,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/mavaali/boundary",
                    "X-Title": "Boundary injection benchmark",
                },
                json=payload,
                timeout=self.timeout,
            )
            if r.status_code >= 400:
                last_err = f"openrouter api {r.status_code}: {r.text[:500]}"
            else:
                data = r.json()
                if "choices" in data and data["choices"]:
                    break
                err = data.get("error") or data
                last_err = f"openrouter: no choices: {str(err)[:500]}"
            if attempt == 0:
                time.sleep(1.0)
        else:
            raise RuntimeError(last_err or "openrouter: unknown error")
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
