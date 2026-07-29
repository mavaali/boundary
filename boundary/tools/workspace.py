from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path


class Workspace:
    """Jails file operations to a root directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        if p.is_absolute():
            resolved = p.resolve()
        else:
            resolved = (self.root / p).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as e:
            raise PermissionError(
                f"path {p!r} escapes the workspace"
            ) from e
        return resolved

    def secure_open(self, path: str | Path, mode: str):
        """Open a workspace file after the containment check, with O_NOFOLLOW on
        the final component so a symlink swapped in AFTER `resolve()` (a
        check-then-open TOCTOU, e.g. under best-of-K concurrency) fails with
        ELOOP instead of following out of the jail. Supported modes: 'rb', 'w',
        'a'. On platforms without O_NOFOLLOW (Windows, where creating a symlink
        needs privilege anyway) it falls back to a normal open — resolve() still
        blocks a final symlink that points outside. Residual: a parent DIRECTORY
        swapped after resolve() is not caught (would need per-component openat)."""
        resolved = self.resolve(path)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            return open(resolved, "rb") if mode == "rb" else open(resolved, mode, encoding="utf-8")
        flags = {
            "rb": os.O_RDONLY,
            "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        }[mode] | nofollow
        fd = os.open(resolved, flags, 0o644)
        return os.fdopen(fd, "rb") if mode == "rb" else os.fdopen(fd, mode, encoding="utf-8")

    def contains(self, path: str | Path) -> bool:
        """True iff `path`'s real location is inside the workspace root.

        Resolves symlinks first, so a link that points outside the root is not
        considered contained even though its own name sits under the root.
        """
        try:
            Path(path).resolve().relative_to(self.root)
            return True
        except (ValueError, OSError):
            return False

    @staticmethod
    def glob_escapes(pattern: str) -> bool:
        """True if a glob pattern would search outside the workspace on its face
        (absolute path, Windows drive, or a '..' component). Such patterns are
        rejected before globbing — the per-result `contains` check is the second
        line for symlinked matches."""
        norm = str(pattern).replace("\\", "/")
        if norm.startswith("/") or norm.startswith("~"):
            return True
        if len(norm) >= 2 and norm[1] == ":":  # e.g. C:\...
            return True
        return ".." in norm.split("/")

    def safe_glob(self, pattern: str) -> Iterator[Path]:
        """Yield files matching `pattern`, every result confined to the root.

        Unlike a bare `root.glob(pattern)`, this refuses patterns that escape on
        their face (raises PermissionError) and drops any match whose real path
        resolves outside the workspace (e.g. through a planted symlink)."""
        if self.glob_escapes(pattern):
            raise PermissionError(
                f"glob pattern {pattern!r} escapes the workspace"
            )
        for p in self.root.glob(pattern):
            if p.is_file() and self.contains(p):
                yield p
