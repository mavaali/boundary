"""Discover — the work-finder that turns Boundary from a dispatcher into a loop.

The 5-beat cycle is Discover -> Assign -> Verify -> Persist -> Decide. Boundary
had Assign/Verify/Persist/Decide; this is the missing Discover beat: something
fires, scans a source, and emits a task per piece of work worth doing. The loop
then fans each task out (Assign) through existing dispatch.

Sources are pluggable. The built-in `markers` source scans a workspace for an
inline marker (default `BOUNDARY-TASK:`) and emits one task per hit — zero
external dependencies, so it is unit-testable and safe to schedule. Real loops
add issue/feedback/email sources by registering another scanner.

Dry-run is the default: discover lists work, it does not act. Pass a dispatch_fn
(or --dispatch on the CLI) to fan out.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MARKER = "BOUNDARY-TASK:"
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".boundary"}


@dataclass
class DiscoveredTask:
    source: str
    title: str
    detail: str
    origin: str = ""  # file:line or external id


def scan_markers(workspace: str | Path, *, marker: str = DEFAULT_MARKER,
                 globs: tuple[str, ...] = ("*.md", "*.py", "*.txt"),
                 max_tasks: int = 25) -> list[DiscoveredTask]:
    ws = Path(workspace).expanduser()
    out: list[DiscoveredTask] = []
    for g in globs:
        for f in ws.rglob(g):
            if any(part in _SKIP_DIRS for part in f.parts):
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if marker in line:
                        text = line.split(marker, 1)[1].strip()
                        out.append(DiscoveredTask(
                            source="markers", title=text[:80] or "(empty)",
                            detail=text, origin=f"{f.relative_to(ws)}:{i}"))
                        if len(out) >= max_tasks:
                            return out
            except (UnicodeDecodeError, OSError):
                continue
    return out


SOURCES: dict[str, Callable[..., list[DiscoveredTask]]] = {"markers": scan_markers}


@dataclass
class DiscoveryResult:
    tasks: list[DiscoveredTask]
    dispatched: list[dict] = field(default_factory=list)


def _ensure_optional_source(source: str) -> None:
    """Lazy-register built-in optional sources that live in their own modules
    (kept out of the top-level import to avoid a circular import)."""
    if source == "fabricspecs_questions" and source not in SOURCES:
        import boundary.sources_fabricspecs  # noqa: F401  (registers on import)


def discover(workspace, *, source: str = "markers", max_tasks: int = 25, **kw) -> list[DiscoveredTask]:
    _ensure_optional_source(source)
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source} (have {sorted(SOURCES)})")
    return SOURCES[source](workspace, max_tasks=max_tasks, **kw)


def run_discovery(workspace, *, source: str = "markers", max_tasks: int = 25,
                  dispatch_fn: Callable[[DiscoveredTask], dict] | None = None,
                  **kw) -> DiscoveryResult:
    """Discover work, then fan out via dispatch_fn (dry-run if None)."""
    tasks = discover(workspace, source=source, max_tasks=max_tasks, **kw)
    dispatched = [dispatch_fn(t) for t in tasks] if dispatch_fn else []
    return DiscoveryResult(tasks=tasks, dispatched=dispatched)
