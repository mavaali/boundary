from __future__ import annotations

from pathlib import Path

import yaml

from boundary.overlay import Overlay


def _write_overlay(tmp_path: Path) -> Path:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "reviewer.md").write_text("# Reviewer\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    overlay_dir = tmp_path / "overlays" / "sample"
    overlay_dir.mkdir(parents=True)
    overlay_path = overlay_dir / "overlay.yaml"
    overlay_path.write_text(yaml.safe_dump({
        "name": "sample",
        "default_workspace": "../../workspace",
        "roles": {"reviewer": "../../prompts/reviewer.md"},
    }))
    return overlay_path


def test_overlay_relative_role_paths_resolve_from_overlay_file(tmp_path):
    overlay = Overlay.load(str(_write_overlay(tmp_path)))

    assert overlay.resolve_role("reviewer") == (tmp_path / "prompts" / "reviewer.md").resolve()


def test_overlay_relative_default_workspace_resolves_from_overlay_file(tmp_path):
    overlay = Overlay.load(str(_write_overlay(tmp_path)))

    assert overlay.workspace_or(None) == str((tmp_path / "workspace").resolve())


def test_repo_sample_overlay_resolves_by_name():
    overlay = Overlay.load("sample")

    assert overlay.name == "sample"
    assert overlay.resolve_role("repo-reviewer").name == "repo-reviewer.md"
    assert overlay.workspace_or(None).replace("\\", "/").endswith("examples/workspaces/sample-repo")
