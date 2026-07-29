"""Jailed-caller launcher: make the MCP gateway's containment involuntary.

`boundary mcp-serve` alone is a boundary by convention — it only binds if the
caller was launched with its native exec/write tools stripped, and one wrong
flag bypasses it. `boundary launch` closes that gap by inverting who holds the
jail:

1. Start the gateway OUTSIDE the sandbox, on loopback HTTP with a fresh
   bearer token (a stdio child would inherit the caller's jail and lose the
   very write access it is supposed to mediate).
2. Run the caller CLI under srt with: workspace reads allowed, workspace
   writes DENIED at the OS, writes allowed only in a throwaway scratch HOME,
   secret paths hidden, and network egress limited to the model API domains
   plus loopback (the gateway).

The caller can then mutate the workspace only through the gateway's
envelope-enforced tools — not because it was asked nicely, but because the OS
refuses everything else. No srt ⇒ refuse to launch (fail closed);
`--allow-uncontained` is the loud, explicit downgrade back to convention.

Placeholders substituted into the caller argv (also exported as env vars
BOUNDARY_MCP_URL / BOUNDARY_MCP_TOKEN / BOUNDARY_MCP_CONFIG):
- `{MCP_URL}`    — the gateway endpoint, e.g. http://127.0.0.1:PORT/mcp
- `{MCP_TOKEN}`  — the session's bearer token
- `{MCP_CONFIG}` — path to a generated Claude Code-style .mcp.json wiring the
                   gateway in as an HTTP server named "boundary"
- `{WORKSPACE}`  — the resolved workspace root
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from boundary.mcp_gateway import make_token
from boundary.tools.sandbox import default_deny_read

LOOPBACK_DOMAINS = ["localhost", "127.0.0.1"]
READY_TIMEOUT_S = 15.0

# Env var names matching these are stripped from the jailed caller's environment
# (F13): they typically hold credentials, and srt's file denyRead cannot hide an
# env var from a process it exec's. Operators re-admit specific ones the caller
# needs (e.g. the caller's own model API key) via --keep-env.
_CREDENTIAL_ENV_RE = re.compile(
    r"(_TOKEN|_KEY|_SECRET|_PASSWORD|_PASSWD|_CREDENTIALS?|_SESSION|_AUTH)$"
    r"|^(AWS_|GH_|GITHUB_|OPENAI_|ANTHROPIC_|GOOGLE_|GCP_|AZURE_|SLACK_|STRIPE_|"
    r"NPM_|PYPI_|DOCKER_|KUBE_|DATABASE_URL|DB_)",
    re.IGNORECASE,
)


def _is_credential_env(name: str) -> bool:
    return bool(_CREDENTIAL_ENV_RE.search(name))


def strip_credential_env(base: dict, keep: list[str] | None = None) -> tuple[dict, list[str]]:
    """Return (filtered_env, stripped_names). Drops credential-shaped var names
    unless in `keep`. Case-insensitive keep matching."""
    keep_lower = {k.lower() for k in (keep or [])}
    filtered, stripped = {}, []
    for k, v in base.items():
        if _is_credential_env(k) and k.lower() not in keep_lower:
            stripped.append(k)
        else:
            filtered[k] = v
    return filtered, sorted(stripped)


def pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_ready(host: str, port: int, timeout: float = READY_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection((host, port), timeout=0.25).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def caller_srt_settings(
    workspace_root: Path,
    scratch: Path,
    egress_allow: list[str],
    deny_read: list[str] | None = None,
) -> dict:
    """srt settings for the CALLER — the inverse of the bash-tool jail.

    The bash jail allows writes to the workspace and nothing else; the caller
    jail DENIES writes to the workspace (mutations must round-trip through the
    gateway) and allows them only in the scratch HOME. denyWrite is listed
    explicitly even though allowWrite wouldn't cover the workspace, so the
    intent survives an srt default change (deny must win over any allow).
    """
    return {
        "network": {
            "allowedDomains": list(egress_allow) + LOOPBACK_DOMAINS,
            "deniedDomains": [],
        },
        "filesystem": {
            "allowRead": ["/"],
            "allowWrite": [str(scratch)],
            "denyRead": default_deny_read() + list(deny_read or []),
            "denyWrite": [str(workspace_root)],
        },
    }


def substitute(argv: list[str], mapping: dict[str, str]) -> list[str]:
    """Replace {MCP_URL}-style placeholders in each caller arg."""
    out = []
    for arg in argv:
        for key, value in mapping.items():
            arg = arg.replace("{" + key + "}", value)
        out.append(arg)
    return out


def write_mcp_config(path: Path, url: str, token: str) -> Path:
    """A Claude Code-style .mcp.json wiring the gateway in over HTTP."""
    config = {
        "mcpServers": {
            "boundary": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def caller_env(base: dict, scratch: Path, mapping: dict[str, str], keep_env: list[str] | None = None) -> dict:
    """Caller environment: HOME/temp repointed into the scratch dir (the only
    writable location), plus the gateway coordinates for callers that read env
    instead of argv placeholders. Credential-shaped var names are stripped
    first (F13) — see `strip_credential_env` — except any listed in
    `keep_env`."""
    tmp = scratch / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    filtered, _stripped = strip_credential_env(base, keep_env)
    env = dict(filtered)
    env.update({
        "HOME": str(scratch),
        "TMPDIR": str(tmp), "TEMP": str(tmp), "TMP": str(tmp),
        "XDG_CACHE_HOME": str(scratch / "cache"),
        "XDG_CONFIG_HOME": str(scratch / "config"),
        "XDG_DATA_HOME": str(scratch / "data"),
        "BOUNDARY_MCP_URL": mapping["MCP_URL"],
        "BOUNDARY_MCP_TOKEN": mapping["MCP_TOKEN"],
        "BOUNDARY_MCP_CONFIG": mapping["MCP_CONFIG"],
    })
    return env


def gateway_argv(args, port: int) -> list[str]:
    """The mcp-serve subprocess argv, forwarding the envelope flags."""
    argv = [
        sys.executable, "-m", "boundary.cli", "mcp-serve",
        "--transport", "http", "--host", "127.0.0.1", "--port", str(port),
        "--workspace", args.workspace,
        "--envelope-max-writes", str(args.envelope_max_writes),
        "--envelope-min-writes", str(args.envelope_min_writes),
        "--envelope-max-appends", str(args.envelope_max_appends),
        "--envelope-max-external", str(args.envelope_max_external),
        "--envelope-max-unstaged-reads", str(args.envelope_max_unstaged_reads),
        "--on-taint", args.on_taint,
        "--on-commit", args.on_commit,
    ]
    for w in args.envelope_writable:
        argv += ["--envelope-writable", w]
    for c in args.commit_allow:
        argv += ["--commit-allow", c]
    if args.no_staging_gate:
        argv.append("--no-staging-gate")
    if args.shell:
        argv.append("--shell")
    return argv


def launch(args, caller: list[str]) -> int:
    """Run `caller` jailed against a live gateway; return the caller's exit code."""
    if not caller:
        print("ERROR: no caller command given — pass it after `--`, e.g. "
              "`boundary launch ... -- claude -p \"task\" --mcp-config {MCP_CONFIG}`",
              file=sys.stderr)
        return 2

    srt = shutil.which("srt")
    if not srt and not args.allow_uncontained:
        print(
            "ERROR: srt is not installed, so the caller jail cannot be enforced "
            "and the gateway would be a boundary by convention only (one caller "
            "flag away from bypass). Install it (`npm i -g "
            "@anthropic-ai/sandbox-runtime`) or pass --allow-uncontained to "
            "accept the downgrade explicitly.",
            file=sys.stderr,
        )
        return 2

    workspace_root = Path(args.workspace).expanduser().resolve()
    port = args.port or pick_free_port()
    token = make_token()
    url = f"http://127.0.0.1:{port}/mcp"

    gw_env = dict(os.environ, BOUNDARY_MCP_TOKEN=token)
    gw = subprocess.Popen(gateway_argv(args, port), env=gw_env)
    scratch = Path(tempfile.mkdtemp(prefix="boundary-launch-"))
    settings_path = scratch / "srt-settings.json"
    try:
        if not wait_ready("127.0.0.1", port):
            print("ERROR: gateway did not come up within "
                  f"{READY_TIMEOUT_S:.0f}s — aborting launch.", file=sys.stderr)
            return 2

        mapping = {
            "MCP_URL": url,
            "MCP_TOKEN": token,
            "MCP_CONFIG": str(write_mcp_config(scratch / "mcp.json", url, token)),
            "WORKSPACE": str(workspace_root),
        }
        caller_cmd = substitute(caller, mapping)
        env = caller_env(os.environ, scratch, mapping, keep_env=getattr(args, "keep_env", None))
        _, stripped = strip_credential_env(os.environ, getattr(args, "keep_env", None))
        if stripped:
            print(f"[boundary launch] stripped {len(stripped)} credential-shaped env "
                  f"var(s) from the jailed caller: {', '.join(stripped)}. If the caller "
                  f"needs one (e.g. its model API key), re-admit it with --keep-env NAME.",
                  file=sys.stderr)

        if srt:
            settings_path.write_text(json.dumps(caller_srt_settings(
                workspace_root, scratch, args.egress_allow, args.deny_read,
            )), encoding="utf-8")
            caller_cmd = [srt, "-s", str(settings_path)] + caller_cmd
            print(f"[boundary launch] caller jailed under srt: workspace writes "
                  f"denied at the OS, egress limited to "
                  f"{args.egress_allow + LOOPBACK_DOMAINS}", file=sys.stderr)
        else:
            print("[boundary launch] WARNING: --allow-uncontained — the caller "
                  "runs UNJAILED; the gateway is a boundary by convention only.",
                  file=sys.stderr)

        result = subprocess.run(caller_cmd, env=env, cwd=str(workspace_root))
        return result.returncode
    finally:
        gw.terminate()
        try:
            gw.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gw.kill()
        shutil.rmtree(scratch, ignore_errors=True)
