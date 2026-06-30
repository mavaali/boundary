"""Persisted, coarse, file-granular taint ledger for one workspace.

Records untrusted *sources* and the set of *tainted files* for a workspace.
Stored under $BOUNDARY_HOME/taint/<hash>.json (default ~/.boundary) — OUTSIDE the
workspace, so the jailed agent (and HOME-repointed sandboxed bash) cannot reach
or clear it. See docs/superpowers/specs/2026-06-20-boundary-taint-ifc-design.md.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

LEDGER_VERSION = 1
_MAX_SOURCES = 200


def _ledger_root() -> Path:
    home = os.environ.get("BOUNDARY_HOME")
    base = Path(home).expanduser() if home else (Path.home() / ".boundary")
    return base / "taint"


def _workspace_hash(workspace_root: Path) -> str:
    return hashlib.sha256(str(workspace_root.resolve()).encode("utf-8")).hexdigest()[:16]


class TaintStore:
    def __init__(self, workspace_root: Path, path: Path,
                 sources: list[str], tainted_files: set[str]):
        self.workspace_root = workspace_root
        self.path = path
        self.sources = sources
        self.tainted_files = tainted_files

    @classmethod
    def load(cls, workspace_root: str | Path) -> TaintStore:
        root = Path(workspace_root).expanduser().resolve()
        ledger = _ledger_root() / f"{_workspace_hash(root)}.json"
        sources: list[str] = []
        files: set[str] = set()
        if ledger.exists():
            try:
                data = json.loads(ledger.read_text(encoding="utf-8"))
                sources = list(data.get("sources", []))
                files = set(data.get("tainted_files", []))
            except (json.JSONDecodeError, OSError):
                pass
        return cls(root, ledger, sources, files)

    def _rel(self, path: str | Path) -> str | None:
        p = Path(path)
        resolved = p.resolve() if p.is_absolute() else (self.workspace_root / p).resolve()
        try:
            return resolved.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return None

    def is_tainted(self, path: str | Path) -> bool:
        rel = self._rel(path)
        return rel is not None and rel in self.tainted_files

    def has_any(self) -> bool:
        return bool(self.tainted_files)

    def mark_source(self, src: str) -> None:
        if src not in self.sources:
            self.sources.append(src)
            self.sources[:] = self.sources[-_MAX_SOURCES:]
            self._save()

    def mark_file(self, path: str | Path) -> None:
        rel = self._rel(path)
        if rel is not None and rel not in self.tainted_files:
            self.tainted_files.add(rel)
            self._save()

    def clear(self) -> None:
        self.sources = []
        self.tainted_files = set()
        try:
            self.path.unlink()
        except OSError:
            pass

    def render(self) -> str:
        lines = [f"taint ledger: {self.path}",
                 f"workspace: {self.workspace_root}",
                 f"sources ({len(self.sources)}):"]
        lines += [f"  - {s}" for s in self.sources] or ["  (none)"]
        lines.append(f"tainted_files ({len(self.tainted_files)}):")
        lines += [f"  - {f}" for f in sorted(self.tainted_files)] or ["  (none)"]
        return "\n".join(lines)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = {
            "version": LEDGER_VERSION,
            "workspace": str(self.workspace_root),
            "sources": self.sources,
            "tainted_files": sorted(self.tainted_files),
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
