"""Boundary — agents inside an explicit envelope.

Exports are lazy (PEP 562) so lightweight consumers — `boundary.kernel` in a
hooks adapter or MCP gateway — can import without pulling the agent loop and
model clients.
"""

__all__ = [
    "Agent",
    "Tool",
    "ToolRegistry",
    "FieldingCoach",
    "ThirdUmpire",
]

_EXPORTS = {
    "Agent": ("boundary.agent", "Agent"),
    "Tool": ("boundary.tools.registry", "Tool"),
    "ToolRegistry": ("boundary.tools.registry", "ToolRegistry"),
    "FieldingCoach": ("boundary.fielding_coach", "FieldingCoach"),
    "ThirdUmpire": ("boundary.third_umpire", "ThirdUmpire"),
}


def __getattr__(name: str):
    try:
        module_name, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module 'boundary' has no attribute {name!r}") from None
    import importlib

    return getattr(importlib.import_module(module_name), attr)
