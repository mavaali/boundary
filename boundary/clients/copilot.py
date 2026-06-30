from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import httpx

from boundary.clients._http import request_with_retry
from boundary.clients.base import ChatResponse, Message, ModelClient, ToolCall

COPILOT_API = "https://api.githubcopilot.com/chat/completions"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
# OAuth app used by Copilot.vim / Copilot.lua / Copilot CLI ecosystem.
# Tokens from `gh auth token` are NOT accepted by /copilot_internal/v2/token;
# only tokens minted by this specific OAuth app are.
COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
APPS_JSON_PATH = Path.home() / ".config" / "github-copilot" / "apps.json"
TOKEN_FILE_MODE = 0o600


def _validate_oauth_token_file_permissions(path: Path = APPS_JSON_PATH) -> None:
    if os.name != "posix" or not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(
            f"Copilot OAuth token file is too permissive: {path} mode {mode:03o}. "
            "Run: chmod 600 ~/.config/github-copilot/apps.json"
        )


def _load_oauth_token_from_disk() -> str | None:
    """Read the Copilot OAuth token from the standard ~/.config/github-copilot/apps.json
    location written by Copilot.vim, Copilot.lua, the gh copilot extension, etc."""
    if not APPS_JSON_PATH.exists():
        return None
    _validate_oauth_token_file_permissions(APPS_JSON_PATH)
    data = json.loads(APPS_JSON_PATH.read_text())
    for val in data.values():
        if isinstance(val, dict) and val.get("oauth_token"):
            return val["oauth_token"]
    return None


def _save_oauth_token_to_disk(oauth_token: str, user: str = "boundary") -> None:
    APPS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if APPS_JSON_PATH.exists():
        try:
            data = json.loads(APPS_JSON_PATH.read_text())
        except json.JSONDecodeError:
            data = {}
    data[f"github.com:{COPILOT_CLIENT_ID}"] = {
        "user": user,
        "oauth_token": oauth_token,
    }
    fd = os.open(APPS_JSON_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2))
        f.write("\n")
    os.chmod(APPS_JSON_PATH, TOKEN_FILE_MODE)
    _validate_oauth_token_file_permissions(APPS_JSON_PATH)


def device_code_login(scope: str = "read:user") -> str:
    """Interactive device-code OAuth flow. Prints code + URL, polls until approval.
    Returns the oauth token and also persists it to apps.json."""
    r = httpx.post(
        DEVICE_CODE_URL,
        headers={"Accept": "application/json"},
        data={"client_id": COPILOT_CLIENT_ID, "scope": scope},
        timeout=30.0,
    )
    r.raise_for_status()
    dc = r.json()
    user_code = dc["user_code"]
    verify_uri = dc["verification_uri"]
    device_code = dc["device_code"]
    interval = dc.get("interval", 5)
    expires_in = dc.get("expires_in", 900)

    print("\n=== GitHub Copilot device-code login ===")
    print(f"  1. Open: {verify_uri}")
    print(f"  2. Enter code: {user_code}")
    print(f"  (waiting up to {expires_in}s, polling every {interval}s)\n")

    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        pr = httpx.post(
            ACCESS_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": COPILOT_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=30.0,
        )
        pd = pr.json()
        if "access_token" in pd:
            tok = pd["access_token"]
            _save_oauth_token_to_disk(tok)
            print(f"[ok] token saved to {APPS_JSON_PATH}")
            return tok
        err = pd.get("error")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                interval += 5
            continue
        raise RuntimeError(f"device-code login failed: {pd}")
    raise TimeoutError("device-code login timed out before user approval")


class CopilotClient(ModelClient):
    """GitHub Copilot chat API client.

    Auth flow:
      1. Look for oauth token in env (COPILOT_OAUTH_TOKEN) or ~/.config/github-copilot/apps.json
      2. If missing, raise — user must run `boundary copilot login` to start the device flow.
      3. Exchange oauth token for a short-lived (~30 min) Copilot chat token, cache, refresh.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4.5",
        oauth_token: str | None = None,
        editor_version: str = "vscode/1.95.0",
        integration_id: str = "vscode-chat",
        timeout: float = 300.0,
    ):
        self.model = model
        self._oauth_token = (
            oauth_token
            or os.environ.get("COPILOT_OAUTH_TOKEN")
            or _load_oauth_token_from_disk()
        )
        if not self._oauth_token:
            raise RuntimeError(
                "No Copilot OAuth token found. Run: `boundary copilot login` "
                "(or set COPILOT_OAUTH_TOKEN, or use Copilot.vim's :Copilot setup)."
            )
        self.editor_version = editor_version
        self.integration_id = integration_id
        self.timeout = timeout
        self._copilot_token: str | None = None
        self._copilot_token_expires: int = 0

    def _refresh_copilot_token(self) -> str:
        now = int(time.time())
        if self._copilot_token and now < self._copilot_token_expires - 60:
            return self._copilot_token
        r = request_with_retry(lambda: httpx.get(
            COPILOT_TOKEN_URL,
            headers={
                "Authorization": f"token {self._oauth_token}",
                "Editor-Version": self.editor_version,
                "Editor-Plugin-Version": "copilot-chat/0.20.0",
                "User-Agent": "GitHubCopilotChat/0.20.0",
                "Accept": "application/json",
            },
            timeout=30.0,
        ))
        r.raise_for_status()
        data = r.json()
        self._copilot_token = data["token"]
        self._copilot_token_expires = data.get("expires_at", now + 1500)
        return self._copilot_token

    def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> ChatResponse:
        token = self._refresh_copilot_token()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        for k in ("temperature", "max_tokens", "top_p"):
            if k in kwargs:
                payload[k] = kwargs[k]

        r = request_with_retry(lambda: httpx.post(
            COPILOT_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Editor-Version": self.editor_version,
                "Copilot-Integration-Id": self.integration_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        ))
        if r.status_code >= 400:
            raise RuntimeError(f"copilot api {r.status_code}: {r.text[:500]}")
        data = r.json()
        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            args = tc["function"]["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    args = {"_raw": args}
            tool_calls.append(
                ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args)
            )
        usage = data.get("usage") or {}
        # OpenAI-format cached tokens live in prompt_tokens_details.cached_tokens
        cached = 0
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens", 0))
        return ChatResponse(
            message=Message(
                role="assistant",
                content=msg.get("content"),
                tool_calls=tool_calls,
            ),
            finish_reason=choice.get("finish_reason", "stop"),
            raw=data,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cached_input_tokens=cached,
        )
