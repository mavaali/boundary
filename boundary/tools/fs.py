from __future__ import annotations
import fnmatch
from pathlib import Path

from boundary.tools.registry import ToolRegistry
from boundary.tools.workspace import Workspace

MAX_READ_BYTES = 200_000


def register_fs_tools(registry: ToolRegistry, workspace: Workspace) -> None:

    @registry.add(
        "read_file",
        "Read a UTF-8 text file from the workspace. Returns up to 200KB.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "relative or absolute path inside workspace"}},
            "required": ["path"],
        },
    )
    def read_file(path: str) -> str:
        p = workspace.resolve(path)
        if not p.exists():
            return f"ERROR: file not found: {path}"
        if not p.is_file():
            return f"ERROR: not a regular file: {path}"
        data = p.read_bytes()[:MAX_READ_BYTES]
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")

    @registry.add(
        "write_file",
        "Write a UTF-8 text file to the workspace. Creates parent dirs. Overwrites if exists. WRITE TOOL — must include a 'reason' field explaining why.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "reason": {"type": "string", "description": "Why this write is needed. Required."},
            },
            "required": ["path", "content", "reason"],
        },
        kind="write",
    )
    def write_file(path: str, content: str, reason: str = "") -> str:
        p = workspace.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {p.relative_to(workspace.root)}"

    @registry.add(
        "append_file",
        "Append content to the end of an existing UTF-8 text file. WRITE TOOL — must include 'reason'. Use this to chunk a long write across multiple tool calls (call write_file once, then append_file for subsequent chunks) so you stay under the per-response output cap. append_file does NOT count against max_writes; it counts against a separate max_appends cap.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "reason": {"type": "string", "description": "Why this append is needed. Required."},
            },
            "required": ["path", "content", "reason"],
        },
        kind="write",
    )
    def append_file(path: str, content: str, reason: str = "") -> str:
        p = workspace.resolve(path)
        if not p.exists():
            return f"ERROR: file not found: {path} — append_file requires a prior write_file"
        if not p.is_file():
            return f"ERROR: not a regular file: {path}"
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"appended {len(content)} chars to {p.relative_to(workspace.root)}"

    @registry.add(
        "edit_file",
        "Replace exactly one occurrence of old_str with new_str. WRITE TOOL — include 'reason'.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
                "reason": {"type": "string", "description": "Why this edit is needed. Required."},
            },
            "required": ["path", "old_str", "new_str", "reason"],
        },
        kind="write",
    )
    def edit_file(path: str, old_str: str, new_str: str, reason: str = "") -> str:
        p = workspace.resolve(path)
        if not p.exists():
            return f"ERROR: file not found: {path}"
        text = p.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            return "ERROR: old_str not found"
        if count > 1:
            return f"ERROR: old_str matches {count} times; needs to be unique"
        p.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        return f"edited {p.relative_to(workspace.root)}"

    @registry.add(
        "list_dir",
        "List entries in a directory (non-recursive). Returns one entry per line with trailing / for dirs.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
        },
    )
    def list_dir(path: str = ".") -> str:
        p = workspace.resolve(path)
        if not p.is_dir():
            return f"ERROR: not a directory: {path}"
        entries = []
        for child in sorted(p.iterdir()):
            entries.append(child.name + ("/" if child.is_dir() else ""))
        return "\n".join(entries) if entries else "(empty)"

    @registry.add(
        "glob",
        "Find files matching a glob pattern (recursive). Returns paths relative to workspace.",
        {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "e.g. **/*.py"}},
            "required": ["pattern"],
        },
    )
    def glob_files(pattern: str) -> str:
        matches = [
            str(p.relative_to(workspace.root))
            for p in workspace.root.glob(pattern)
            if p.is_file()
        ]
        matches.sort()
        return "\n".join(matches[:500]) if matches else "(no matches)"

    @registry.add(
        "grep",
        "Search for a literal substring across files matching a glob. Returns total counts plus matching lines with path:line: prefix. Use this for content search.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string", "default": "**/*"},
                "max_hits": {"type": "integer", "default": 200},
            },
            "required": ["pattern"],
        },
    )
    def grep(pattern: str, glob: str = "**/*", max_hits: int = 200) -> str:
        hits: list[str] = []
        total_hits = 0
        files_with_match: set[str] = set()
        files_scanned = 0
        for p in workspace.root.glob(glob):
            if not p.is_file():
                continue
            files_scanned += 1
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            file_had_match = False
            for i, line in enumerate(text.splitlines(), 1):
                if pattern in line:
                    total_hits += 1
                    file_had_match = True
                    if len(hits) < max_hits:
                        hits.append(f"{p.relative_to(workspace.root)}:{i}:{line[:200]}")
            if file_had_match:
                files_with_match.add(str(p.relative_to(workspace.root)))
        header = (
            f"[grep] pattern={pattern!r} glob={glob!r} "
            f"total_hits={total_hits} files_matched={len(files_with_match)} "
            f"files_scanned={files_scanned} showing={len(hits)}/{max_hits}"
        )
        body = "\n".join(hits) if hits else "(no matches)"
        return header + "\n" + body

    @registry.add(
        "count_matches",
        "Return only the total count of files matching a glob that contain a pattern. Cheaper than grep when you only need numbers.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string", "default": "**/*"},
            },
            "required": ["pattern"],
        },
    )
    def count_matches(pattern: str, glob: str = "**/*") -> str:
        total_hits = 0
        files_with_match = 0
        files_scanned = 0
        for p in workspace.root.glob(glob):
            if not p.is_file():
                continue
            files_scanned += 1
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            n = text.count(pattern)
            if n > 0:
                total_hits += n
                files_with_match += 1
        return (
            f"pattern={pattern!r} glob={glob!r} "
            f"total_hits={total_hits} files_matched={files_with_match} "
            f"files_scanned={files_scanned}"
        )
