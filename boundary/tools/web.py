from __future__ import annotations

import httpx

from boundary.tools.registry import ToolRegistry

MAX_FETCH_BYTES = 200_000


def register_web_tools(registry: ToolRegistry, timeout: float = 30.0) -> None:

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
            r = httpx.get(url, timeout=timeout, follow_redirects=True, headers={
                "User-Agent": "boundary/0.1 (+research)",
            })
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
