"""Regression guard for F5 — fetch_url SSRF.

fetch_url passed the model-supplied URL to httpx with no scheme/host validation
and followed redirects, so it could reach cloud metadata (169.254.169.254),
loopback, and internal hosts, and an allowed host could 302 to an internal one.
It now refuses non-http(s) schemes and any target that resolves to a non-public
address, re-validating on every redirect hop. SSRF protection applies on every
run, not only when an egress allowlist is enforced.
"""
from __future__ import annotations

import httpx

from boundary.tools.registry import ToolRegistry
from boundary.tools.web import _ip_is_blocked, register_web_tools


def _web(allowlist=None, enforce=False):
    reg = ToolRegistry()
    register_web_tools(reg, egress_allowlist=allowlist, enforce_egress=enforce)
    return reg.get("fetch_url").fn


def test_ip_classification():
    for ip in ["169.254.169.254", "127.0.0.1", "10.0.0.1", "192.168.1.1",
               "172.16.0.1", "::1", "0.0.0.0", "::ffff:127.0.0.1", "fc00::1"]:
        assert _ip_is_blocked(ip), ip
    for ip in ["8.8.8.8", "1.1.1.1"]:
        assert not _ip_is_blocked(ip), ip


def test_metadata_and_internal_blocked_without_enforcement():
    # default --web run: no allowlist, enforce_egress=False. SSRF still blocked.
    fetch = _web()
    for url in [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:9/",
        "http://localhost:80/",
        "http://10.0.0.5/x",
    ]:
        out = fetch(url=url, reason="x")
        assert out.startswith("ERROR: egress refused"), (url, out)


def test_non_http_scheme_blocked_without_enforcement():
    fetch = _web()
    assert fetch(url="file:///etc/passwd", reason="x").startswith("ERROR: egress refused")
    assert fetch(url="ftp://host/x", reason="x").startswith("ERROR: egress refused")


def test_allowlisted_internal_host_bypasses_ssrf_guard(monkeypatch):
    # An explicit allowlist entry is an operator opt-in: an internal host that is
    # allowlisted skips the public-address check and is fetched.
    import boundary.tools.web as web
    calls: list[str] = []

    def fake_get(url, **kw):
        calls.append(url)
        return httpx.Response(200, text="internal-ok")

    monkeypatch.setattr(web.httpx, "get", fake_get)
    fetch = _web(allowlist=["10.0.0.5"], enforce=True)
    out = fetch(url="http://10.0.0.5/status", reason="x")
    assert out == "internal-ok"
    assert calls == ["http://10.0.0.5/status"]


def test_non_allowlisted_internal_host_blocked_before_any_request(monkeypatch):
    import boundary.tools.web as web
    calls: list[str] = []
    monkeypatch.setattr(web.httpx, "get",
                        lambda url, **kw: calls.append(url) or httpx.Response(200, text="x"))
    out = _web()(url="http://10.0.0.5/status", reason="x")
    assert out.startswith("ERROR: egress refused")
    assert calls == []  # refused before the network was touched


def test_redirect_to_internal_is_revalidated(monkeypatch):
    # First hop (allowlisted, public) is allowed and 302s to the metadata IP; the
    # second hop must be re-checked and refused before a request is made to it.
    import boundary.tools.web as web
    responses = [httpx.Response(302, headers={"location": "http://169.254.169.254/"})]
    calls: list[str] = []

    def fake_get(url, **kw):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(web.httpx, "get", fake_get)
    fetch = _web(allowlist=["example.com"], enforce=True)
    out = fetch(url="http://example.com/", reason="x")
    assert out.startswith("ERROR: egress refused")
    assert calls == ["http://example.com/"]  # metadata hop never requested
