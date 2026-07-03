from __future__ import annotations

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
                f"path {resolved} escapes workspace {self.root}"
            ) from e
        return resolved

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
                f"glob pattern {pattern!r} escapes workspace {self.root}"
            )
        for p in self.root.glob(pattern):
            if p.is_file() and self.contains(p):
                yield p
