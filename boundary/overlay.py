from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Overlay:
    name: str
    path: Path
    default_workspace: str | None = None
    enable_clawpilot: bool = False
    extra_system: str | None = None
    roles: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, name_or_path: str) -> "Overlay":
        path = _resolve_overlay_path(name_or_path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        roles = {
            str(name).lower(): str(role_path)
            for name, role_path in (data.get("roles") or {}).items()
        }
        return cls(
            name=str(data.get("name") or path.parent.name),
            path=path,
            default_workspace=data.get("default_workspace"),
            enable_clawpilot=bool(data.get("enable_clawpilot", False)),
            extra_system=data.get("extra_system"),
            roles=roles,
        )

    def resolve_role(self, role: str) -> Path:
        key = role.lower().lstrip("/")
        if key not in self.roles:
            known = ", ".join(sorted(self.roles)) or "(none)"
            raise KeyError(f"role {role!r} not found in overlay {self.name!r}; known roles: {known}")
        return _resolve_overlay_relative(self.path.parent, self.roles[key])

    def workspace_or(self, explicit: str | None) -> str:
        if explicit and explicit != ".":
            return explicit
        if self.default_workspace:
            return str(_resolve_overlay_relative(self.path.parent, self.default_workspace))
        return explicit or "."


def _resolve_overlay_path(name_or_path: str) -> Path:
    raw = Path(name_or_path).expanduser()
    candidates: list[Path] = []
    if raw.exists():
        candidates.append(raw / "overlay.yaml" if raw.is_dir() else raw)
    else:
        candidates.extend([
            REPO_ROOT / "overlays" / name_or_path / "overlay.yaml",
            REPO_ROOT / "examples" / "overlays" / name_or_path / "overlay.yaml",
            Path.home() / ".boundary" / "overlays" / name_or_path / "overlay.yaml",
        ])
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    searched = ", ".join(str(c) for c in candidates) or str(raw)
    raise FileNotFoundError(f"overlay not found: {name_or_path!r}; searched: {searched}")


def _resolve_overlay_relative(base: Path, value: str) -> Path:
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw
    return (base / raw).resolve()


def list_overlays() -> list[Path]:
    roots = [
        REPO_ROOT / "overlays",
        REPO_ROOT / "examples" / "overlays",
        Path.home() / ".boundary" / "overlays",
    ]
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        found.extend(sorted(root.glob("*/overlay.yaml")))
    return found
