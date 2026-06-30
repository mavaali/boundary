from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

ToolKind = Literal["read", "write", "external", "commit"]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema
    fn: Callable[..., Any]
    kind: ToolKind = "read"
    # "read"     = pure observation, free
    # "write"    = mutates workspace files, bounded by writable_paths + max_writes
    # "external" = network/subprocess query (no commit), bounded by max_external
    # "commit"   = IRREVERSIBLE external side effect (send, post, push, file).
    #              Headless default = REFUSE. See envelope.Envelope.on_commit.

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def call(self, arguments: dict) -> str:
        result = self.fn(**arguments)
        if isinstance(result, str):
            return result
        import json
        try:
            return json.dumps(result, default=str)
        except Exception:
            return str(result)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def add(self, name: str, description: str, parameters: dict, kind: ToolKind = "read"):
        """Decorator form: @registry.add('foo', 'desc', {...}, kind='write')"""
        def deco(fn):
            self.register(Tool(name, description, parameters, fn, kind=kind))
            return fn
        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        return [t.to_openai() for t in self._tools.values()]

    def by_kind(self, kind: ToolKind) -> list[Tool]:
        return [t for t in self._tools.values() if t.kind == kind]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
