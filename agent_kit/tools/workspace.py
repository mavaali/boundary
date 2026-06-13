from __future__ import annotations
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
        except ValueError:
            raise PermissionError(
                f"path {resolved} escapes workspace {self.root}"
            )
        return resolved
