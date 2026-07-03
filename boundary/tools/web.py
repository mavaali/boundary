from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from boundary.tools.registry import ToolRegistry

MAX_FETCH_BYTES = 200_000
_MAX_REDIRECTS = 5


class _EgressBlocked(Exception):
    """Raised when a request (or a redirect hop) is refused: an off-allowlist
    host, a non-http(s) scheme, or a target that resolves to a non-public
    (SSRF) address. Keeps egress bounded to what the srt sandbox enforces on
    bash and closes the in-process SSRF hole."""


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    """Match `host` against the egress allowlist. An allowlist entry covers that
    exact host and its subdomains (mirrors srt's allowedDomains semantics)."""
    h = (host or "").lower().rstrip(".")
    for entry in allowlist:
        d = entry.lower().strip().rstrip(".")
        if d and (h == d or h.endswith("." + d)):
            return True
    return False


def _ip_is_blocked(ip_str: str) -> bool:
    """True if `ip_str` is not a public unicast address — loopback, link-local
    (incl. the 169.254.169.254 cloud-metadata endpoint), private (RFC1918 / ULA),
    reserved, multicast, or unspecified. Unparseable → blocked (fail closed)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # unwrap ::ffff:127.0.0.1 and friends
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _assert_public_host(host: str) -> None:
    """Resolve `host` and refuse if any resolved address is non-public. Blocking
    on *any* non-public record (not requiring all) is the safe direction against
    DNS-based SSRF. Resolution failure is treated as blocked (fail closed).

    Note: this validates then lets httpx resolve again to connect, so a DNS
    rebinding between the two lookups is a residual — full closure would pin the
    socket to the validated IP. It stops the practical cases: IP literals,
    localhost, and metadata/internal hostnames."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise _EgressBlocked(f"could not resolve host {host!r} to verify it is public ({e})")
    for info in infos:
        ip = info[4][0]
        if _ip_is_blocked(ip):
            raise _EgressBlocked(
                f"host {host!r} resolves to a non-public address ({ip}) — refused as a "
                f"possible SSRF target. If this internal host is intended, allowlist it "
                f"with --egress-allow."
            )


def _safe_get(url: str, allowlist: list[str], enforce: bool, timeout: float) -> httpx.Response:
    """GET `url`, validating scheme, allowlist membership, and target address on
    the initial request and on every redirect hop. Redirects are followed
    manually so an allowed host cannot 302 the request to a refused target.

    - scheme must be http/https (always);
    - when `enforce`, the host must be on the egress allowlist;
    - a host that is NOT explicitly allowlisted must resolve to a public address
      (SSRF guard). An allowlist entry is an operator opt-in that also permits an
      internal address, so allowlisted hosts skip the address check."""
    headers = {"User-Agent": "boundary/0.1 (+research)"}
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https"):
            raise _EgressBlocked(f"scheme {parts.scheme!r} not allowed (http/https only)")
        host = parts.hostname or ""
        if not host:
            raise _EgressBlocked(f"missing host in URL {current!r}")
        allowlisted = _host_allowed(host, allowlist)
        if enforce and not allowlisted:
            raise _EgressBlocked(f"host {host!r} is not in the egress allowlist {allowlist}")
        if not allowlisted:
            _assert_public_host(host)
        r = httpx.get(current, timeout=timeout, follow_redirects=False, headers=headers)
        if r.is_redirect and r.headers.get("location"):
            current = str(httpx.URL(current).join(r.headers["location"]))
            continue
        return r
    raise _EgressBlocked("too many redirects")


def register_web_tools(
    registry: ToolRegistry,
    timeout: float = 30.0,
    egress_allowlist: list[str] | None = None,
    enforce_egress: bool = False,
) -> None:
    # fetch_url makes in-process HTTP calls the srt sandbox never sees, so it
    # enforces its own boundary. SSRF protection (scheme + non-public-address
    # block) applies on every run. When enforce_egress is set (srt driver, or a
    # run that declared an allowlist), the host must also be on the allowlist; an
    # empty allowlist under enforcement means "no network" (fail closed).
    allowlist = list(egress_allowlist or [])

    @registry.add(
        "fetch_url",
        "Fetch a URL and return the response body as text (up to 200KB). EXTERNAL — include 'reason'.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "as_markdown": {"type": "boolean", "default": False},
                "reason": {"type": "string", "description": "Why this fetch is needed. Required."},
            },
            "required": ["url", "reason"],
        },
        kind="external",
    )
    def fetch_url(url: str, as_markdown: bool = False, reason: str = "") -> str:
        try:
            r = _safe_get(url, allowlist, enforce_egress, timeout)
        except _EgressBlocked as e:
            return f"ERROR: egress refused: {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
        if r.status_code >= 400:
            return f"ERROR: http {r.status_code}"
        text = r.text[:MAX_FETCH_BYTES]
        if as_markdown and "<" in text:
            import re
            text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
            text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
