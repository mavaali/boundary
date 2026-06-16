from __future__ import annotations

import json
import os

import pytest

from boundary.clients import copilot


def test_save_oauth_token_uses_private_file_mode(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX file-mode bits only — Windows ignores chmod(0o600)")
    token_path = tmp_path / "github-copilot" / "apps.json"
    monkeypatch.setattr(copilot, "APPS_JSON_PATH", token_path)

    copilot._save_oauth_token_to_disk("secret-token", user="tester")

    mode = token_path.stat().st_mode & 0o777
    assert mode == 0o600
    data = json.loads(token_path.read_text())
    entry = data[f"github.com:{copilot.COPILOT_CLIENT_ID}"]
    assert entry == {"user": "tester", "oauth_token": "secret-token"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_load_oauth_token_refuses_group_or_world_readable_file(tmp_path, monkeypatch):
    token_path = tmp_path / "github-copilot" / "apps.json"
    token_path.parent.mkdir()
    token_path.write_text(json.dumps({
        f"github.com:{copilot.COPILOT_CLIENT_ID}": {"oauth_token": "secret-token"},
    }))
    token_path.chmod(0o644)
    monkeypatch.setattr(copilot, "APPS_JSON_PATH", token_path)

    with pytest.raises(PermissionError, match="too permissive"):
        copilot._load_oauth_token_from_disk()


def test_load_oauth_token_reads_private_file(tmp_path, monkeypatch):
    token_path = tmp_path / "github-copilot" / "apps.json"
    token_path.parent.mkdir()
    token_path.write_text(json.dumps({
        f"github.com:{copilot.COPILOT_CLIENT_ID}": {"oauth_token": "secret-token"},
    }))
    token_path.chmod(0o600)
    monkeypatch.setattr(copilot, "APPS_JSON_PATH", token_path)

    assert copilot._load_oauth_token_from_disk() == "secret-token"
