"""Unit tests for credential-shaped env var stripping (F13).

Covers `strip_credential_env` directly and its use inside `caller_env`, the
jailed-caller environment builder in boundary/launcher.py.
"""
from __future__ import annotations

from boundary.launcher import caller_env, strip_credential_env


def test_strip_credential_env_drops_credential_shaped_names():
    base = {
        "AWS_SECRET_ACCESS_KEY": "aaa",
        "GITHUB_TOKEN": "ghp_xxx",
        "MY_API_KEY": "mykey",
        "DATABASE_URL": "postgres://...",
        "FOO_PASSWORD": "hunter2",
    }
    filtered, stripped = strip_credential_env(base)
    assert filtered == {}
    assert set(stripped) == set(base.keys())


def test_strip_credential_env_passes_through_normal_names():
    base = {"PATH": "/bin", "LANG": "en_US.UTF-8", "HOME": "/home/x",
            "TERM": "xterm", "EDITOR": "vim"}
    filtered, stripped = strip_credential_env(base)
    assert filtered == base
    assert stripped == []


def test_strip_credential_env_keep_env_readmits_case_insensitively():
    base = {
        "ANTHROPIC_API_KEY": "sk-ant-xxx",
        "GITHUB_TOKEN": "ghp_xxx",
        "PATH": "/bin",
    }
    filtered, stripped = strip_credential_env(base, keep=["anthropic_api_key"])
    assert filtered["ANTHROPIC_API_KEY"] == "sk-ant-xxx"
    assert filtered["PATH"] == "/bin"
    assert "GITHUB_TOKEN" not in filtered
    assert stripped == ["GITHUB_TOKEN"]


def test_caller_env_strips_credentials_and_applies_overrides(tmp_path):
    scratch = tmp_path / "scratch"
    mapping = {"MCP_URL": "http://u", "MCP_TOKEN": "t", "MCP_CONFIG": "/c",
               "WORKSPACE": "/ws"}
    base = {
        "PATH": "/bin",
        "GITHUB_TOKEN": "ghp_xxx",
        "ANTHROPIC_API_KEY": "sk-ant-xxx",
    }
    env = caller_env(base, scratch, mapping, keep_env=["ANTHROPIC_API_KEY"])
    assert "GITHUB_TOKEN" not in env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-xxx"
    assert env["HOME"] == str(scratch)
    assert env["BOUNDARY_MCP_TOKEN"] == "t"
    assert env["PATH"] == "/bin"
