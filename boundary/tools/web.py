from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from boundary.tools.registry import ToolRegistry

MAX_FETCH_BYTES = 200_000
_MAX_REDIRECTS = 5


class _EgressBlocked(Exception):
    """Raised when a request (or a redirect hop) targets a host outside the
    egress allowlist, so egress stays bounded to the same allowlist the srt
    sandbox enforces on bash."""


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    """Match `host` against the egress allowlist. An allowlist entry covers that
    exact host and its subdomains (mirrors srt's allowedDomains semantics)."""
    h = (host or "").lower().rstrip(".")
    for entry in allowlist:
        d = entry.lower().strip().rstrip(".")
        if d and (h == d or h.endswith("." + d)):
            return True
    return False


def _get_contained(url: str, allowlist: list[str], timeout: float) -> httpx.Response:
    """GET `url`, validating scheme and host against the allowlist on the initial
    request and on every redirect hop. Redirects are followed manually so an
    allowlisted host cannot 302 the request to an off-allowlist target."""
    headers = {"User-Agent": "boundary/0.1 (+research)"}
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https"):
            raise _EgressBlocked(f"scheme {parts.scheme!r} not allowed (http/https only)")
        if not _host_allowed(parts.hostname or "", allowlist):
            raise _EgressBlocked(
                f"host {parts.hostname!r} is not in the egress allowlist {allowlist}"
            )
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
    # When enforce_egress is set (srt driver, or any run that declared an egress
    # allowlist), fetch_url is bounded to that allowlist in-process — the srt
    # sandbox only wraps bash, so without this an in-process httpx call would be
    # an unbounded egress/exfil channel. An empty allowlist under enforcement
    # means "no network" (fail closed), matching srt's allowedDomains=[].
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
            if enforce_egress:
                r = _get_contained(url, allowlist, timeout)
            else:
                r = httpx.get(url, timeout=timeout, follow_redirects=True, headers={
                    "User-Agent": "boundary/0.1 (+research)",
                })
        except _EgressBlocked as e:
            return (
                f"ERROR: egress refused: {e}. Network egress is bounded to the "
                f"run's allowlist; re-scope with --egress-allow to permit this host."
            )
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
