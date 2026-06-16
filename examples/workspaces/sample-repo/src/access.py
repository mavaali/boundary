from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: str
    active: bool
    is_admin: bool
    project_ids: tuple[str, ...]


def can_read_project(user: User, project_id: str) -> bool:
    if not user.active:
        return False
    if user.is_admin:
        return True
    return project_id in user.project_ids

