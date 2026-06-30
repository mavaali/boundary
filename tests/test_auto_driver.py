"""The 'auto' sandbox driver — secure-by-default with a graceful, loud fallback.

auto prefers srt (OS-enforced egress containment). When srt is absent it falls
back to seatbelt on macOS WITH a warning that egress is uncontained, and refuses
entirely elsewhere rather than silently dropping the jail. These tests stub
`shutil.which` / `platform.system` so they're deterministic on any host.
"""
from __future__ import annotations

import boundary.tools.sandbox as sb
from boundary.tools.sandbox import SANDBOX_DRIVERS, resolve_auto_driver


def _reset_warn():
    sb._AUTO_WARNED = False


def test_auto_is_a_known_driver():
    assert "auto" in SANDBOX_DRIVERS


def test_auto_prefers_srt_when_available(monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda name: "/usr/bin/srt")
    driver, warning = resolve_auto_driver()
    assert driver == "srt"
    assert warning is None  # the secure path is silent


def test_auto_falls_back_to_seatbelt_on_macos_with_warning(monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    monkeypatch.setattr(sb.platform, "system", lambda: "Darwin")
    driver, warning = resolve_auto_driver()
    assert driver == "seatbelt"
    assert warning and "egress is NOT contained" in warning


def test_auto_refuses_when_no_sandbox_available(monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    driver, warning = resolve_auto_driver()
    assert driver is None  # caller turns this into a hard error


def test_run_sandboxed_auto_errors_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    out = sb.run_sandboxed("echo hi", workspace_root=tmp_path, timeout=5, driver="auto")
    assert out.startswith("ERROR: no OS sandbox available")


def test_run_sandboxed_auto_warns_once(monkeypatch, tmp_path, capsys):
    # Fall back to seatbelt but stub the seatbelt runner so we don't actually
    # shell out — we only care that the warning fires exactly once.
    _reset_warn()
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    monkeypatch.setattr(sb.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sb, "_run_seatbelt", lambda *a, **k: "[exit 0]\nok")
    sb.run_sandboxed("echo hi", workspace_root=tmp_path, timeout=5, driver="auto")
    sb.run_sandboxed("echo hi", workspace_root=tmp_path, timeout=5, driver="auto")
    err = capsys.readouterr().err
    assert err.count("WARNING") == 1  # warn-once, not per-call


def test_agent_resolves_auto_to_concrete_driver(monkeypatch, tmp_path):
    # Agent must store the RESOLVED driver so the transcript and the Third
    # Umpire's egress check see what actually ran, never the literal "auto".
    monkeypatch.setattr(sb.shutil, "which", lambda name: "/usr/bin/srt")
    from boundary.agent import Agent

    a = Agent(
        name="t", system_prompt="s", workspace=tmp_path,
        client="copilot", sandbox_driver="auto",
        enable_shell=False, transcript=False,
        client_kwargs={"oauth_token": "x"},
    )
    assert a.sandbox_driver == "srt"
