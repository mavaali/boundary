"""MCP gateway: Boundary's envelope-enforced tools served over the Model
Context Protocol (stdio), for the tool-inversion architecture in
docs/codex-orchestration-plan.md.

An external agent CLI (Codex, Claude Code, anything MCP-capable) is launched
with its native exec/write tools stripped and this server registered instead.
The model proposes; Boundary executes — every call passes through the SAME
`_make_enforced_tool` gate the engine uses (write allowlist + floor/ceiling,
staging pivot, taint ledger, commit policy, fail-closed unknown-write refusal),
so the enforcement semantics cannot drift from the engine's.

What this mode does NOT enforce: token/dollar spend. The caller's model spends
tokens in the caller's process — this server never sees them. Cap spend on the
caller (`claude -p --max-budget-usd`, Console workspace limits) and treat the
envelope here as the write/read/egress boundary. Spend caps passed on the
Envelope are ignored, mirroring the Claude Code plugin's honesty about the same
limitation.

The `Gateway` core has no dependency on the `mcp` package and is what the tests
exercise; only `serve_stdio()` imports `mcp` (install via
`pip install boundary-envelope[mcp]`).
"""
from __future__ import annotations

import json
from pathlib import Path

from boundary.envelope import (
    Envelope,
    EnvelopeEvent,
    _make_enforced_tool,
    _prevalidate_call,
    _stage_proposal_tool,
    classify_tool_result,
)
from boundary.taint import TaintStore
from boundary.tools.fs import register_fs_tools
from boundary.tools.registry import Tool, ToolRegistry
from boundary.tools.sandbox import resolve_auto_driver, warn_once
from boundary.tools.shell import register_shell_tools
from boundary.tools.workspace import Workspace
from boundary.transcript import Transcript

# Every served tool carries this prefix so the name stays self-identifying in
# clients that flatten multi-server tool lists (per MCP naming guidance).
TOOL_PREFIX = "boundary_"
STATUS_TOOL = "boundary_status"
SERVER_NAME = "boundary"
_EVENTS_TAIL = 50


class Gateway:
    """Envelope-enforced tool registry with a per-session ledger.

    One Gateway == one envelope session: counters (writes, unstaged reads,
    taint) accumulate across every MCP call for the server's lifetime, exactly
    as they would across one EnvelopeRunner loop. Restarting the server starts
    a fresh session; the TaintStore persists across sessions by design.
    """

    def __init__(
        self,
        workspace: str | Path,
        envelope: Envelope,
        *,
        enable_shell: bool = False,
        shell_timeout: int = 60,
        sandbox_driver: str = "auto",
        egress_allowlist: list[str] | None = None,
        deny_read: list[str] | None = None,
        transcript: Transcript | bool | None = True,
    ):
        self.envelope = envelope
        self.workspace = Workspace(workspace)
        self.egress_allowlist = list(egress_allowlist or [])
        # Resolve "auto" up front so the transcript and taint gate see the
        # driver that actually runs, not "auto" (same reasoning as Agent).
        if sandbox_driver == "auto":
            resolved, warning = resolve_auto_driver()
            if warning and enable_shell:
                warn_once(warning)
            sandbox_driver = resolved or "auto"
        self.sandbox_driver = sandbox_driver

        base = ToolRegistry()
        register_fs_tools(base, self.workspace)
        if enable_shell:
            register_shell_tools(
                base, self.workspace, timeout=shell_timeout, allow=True,
                driver=sandbox_driver, egress_allowlist=egress_allowlist,
                deny_read=list(deny_read or []),
            )

        self._counters: dict[str, int] = {}
        self._events: list[EnvelopeEvent] = []
        self._iter_ref = [0]
        self._store = TaintStore.load(self.workspace.root)
        self._registry = ToolRegistry()
        for tool in base._tools.values():
            self._registry.register(_make_enforced_tool(
                tool, envelope, self._counters, self._events, self._iter_ref,
                store=self._store,
                sandbox_driver=self.sandbox_driver,
                egress_allowlist=self.egress_allowlist,
            ))
        if envelope.require_staging and envelope.writable_paths:
            self._registry.register(
                _stage_proposal_tool(self._counters, self._events, self._iter_ref)
            )

        if transcript is True:
            self.transcript: Transcript | None = Transcript(agent_name="mcp-gateway")
        elif transcript is False or transcript is None:
            self.transcript = None
        else:
            self.transcript = transcript
        if self.transcript:
            self.transcript.log(
                "envelope_start",
                writable_paths=envelope.writable_paths,
                max_writes=envelope.max_writes,
                min_writes=envelope.min_writes,
                max_appends=envelope.max_appends,
                require_staging=envelope.require_staging,
                max_unstaged_reads=envelope.max_unstaged_reads,
                write_profile=envelope.write_profile,
                spec=envelope.spec_dict(),
                spec_hash=envelope.spec_hash(),
                task="(mcp session)",
                workspace=str(self.workspace.root),
                sandbox_driver=self.sandbox_driver,
            )

    def list_tools(self) -> list[dict]:
        """Served tool definitions: {name, description, inputSchema}."""
        tools = [
            {
                "name": TOOL_PREFIX + t.name,
                "description": t.description,
                "inputSchema": t.parameters,
            }
            for t in self._registry._tools.values()
        ]
        tools.append({
            "name": STATUS_TOOL,
            "description": (
                "Show this session's envelope: the enforced spec, live counters "
                "(writes, appends, unstaged reads, taint), and recent envelope "
                "events. Read-only; costs nothing against any cap."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        })
        return tools

    def status(self) -> dict:
        c = self._counters
        return {
            "spec": self.envelope.spec_dict(),
            "spec_hash": self.envelope.spec_hash(),
            "workspace": str(self.workspace.root),
            "sandbox_driver": self.sandbox_driver,
            "counters": {
                "calls": self._iter_ref[0],
                "writes_attempted": c.get("writes_attempted", 0),
                "writes_executed": c.get("writes_executed", 0),
                "appends_executed": c.get("appends_executed", 0),
                "external_calls": c.get("external_calls", 0),
                "unstaged_reads": c.get("unstaged_reads", 0),
                "staged": bool(c.get("staged", 0)),
                "tainted_reads": c.get("tainted_reads", 0),
            },
            "min_writes_met": c.get("writes_executed", 0) >= self.envelope.min_writes,
            "events": [
                {"kind": e.kind, "tool": e.tool, "detail": e.detail, "iteration": e.iteration}
                for e in self._events[-_EVENTS_TAIL:]
            ],
            "spend_note": (
                "token/dollar spend is NOT metered here — the caller's model "
                "spends in the caller's process; cap it there"
            ),
        }

    def call(self, name: str, arguments: dict | None) -> str:
        """Dispatch one MCP tool call through the enforced registry.

        Always returns a string (the engine's tool-result convention);
        enforcement refusals come back as 'ENVELOPE REFUSED: ...' and
        malformed calls as 'ERROR: ...', so the calling model gets the same
        typed feedback an engine-loop agent would.
        """
        arguments = arguments or {}
        if name == STATUS_TOOL:
            return json.dumps(self.status(), indent=2, default=str)
        bare = name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name
        tool: Tool | None = self._registry.get(bare)
        if tool is None:
            served = ", ".join(t["name"] for t in self.list_tools())
            return f"ERROR: unknown tool {name!r}. Served tools: {served}"
        self._iter_ref[0] += 1
        i = self._iter_ref[0]
        invalid = _prevalidate_call(tool, arguments)
        if invalid is not None:
            result, raised = invalid, None
        else:
            try:
                result, raised = tool.call(arguments), None
            except Exception as e:
                result, raised = f"ERROR: {type(e).__name__}: {e}", e
        result_class = classify_tool_result(result, raised)
        if self.transcript:
            self.transcript.log(
                "tool_result", iteration=i, tool=bare,
                result=result[:2000], result_class=result_class,
            )
        return result

    def close(self) -> None:
        if self.transcript:
            c = self._counters
            self.transcript.log(
                "envelope_end",
                writes_attempted=c.get("writes_attempted", 0),
                writes_executed=c.get("writes_executed", 0),
                appends_executed=c.get("appends_executed", 0),
                external_calls=c.get("external_calls", 0),
                unstaged_reads=c.get("unstaged_reads", 0),
                staged=bool(c.get("staged", 0)),
                tainted_reads=c.get("tainted_reads", 0),
                calls=self._iter_ref[0],
            )
            self.transcript.close()


def _require_mcp():
    try:
        import mcp.types as types  # noqa: F401
        from mcp.server import Server  # noqa: F401
    except ImportError as e:
        raise SystemExit(
            "the MCP gateway needs the optional 'mcp' package: "
            "pip install 'boundary-envelope[mcp]'"
        ) from e


def _build_server(gateway: Gateway):
    """Lowlevel MCP Server (SDK 2.x constructor-handler API) over the gateway."""
    import mcp.types as types
    from mcp.server import Server

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[
            types.Tool(
                name=t["name"], description=t["description"], inputSchema=t["inputSchema"],
            )
            for t in gateway.list_tools()
        ])

    async def on_call_tool(ctx, params) -> types.CallToolResult:
        # Envelope refusals and arg errors travel as ordinary text results, not
        # protocol errors — the calling model must read them and self-correct,
        # exactly as an engine-loop agent would.
        text = gateway.call(params.name, params.arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
        )

    return Server(
        SERVER_NAME,
        instructions=(
            "Boundary envelope gateway: file and shell tools jailed to one "
            "workspace, enforced at this server (write allowlist, write "
            "floor/ceiling, staging pivot, taint ledger, commit policy). A "
            "result starting with 'ENVELOPE REFUSED' is a policy refusal — "
            "change approach instead of retrying. Call boundary_status to see "
            "the live envelope."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve_stdio(gateway: Gateway) -> None:
    """Serve the gateway over MCP stdio. Requires the optional `mcp` package."""
    _require_mcp()
    from mcp.server.stdio import stdio_server

    server = _build_server(gateway)
    async with stdio_server() as (read_stream, write_stream):
        try:
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )
        finally:
            gateway.close()


def make_token() -> str:
    """A fresh shared-secret bearer token for the HTTP transport."""
    import secrets
    return secrets.token_urlsafe(32)


class BearerAuthASGI:
    """Shared-secret bearer gate wrapped around the streamable-HTTP app.

    Deliberately NOT the SDK's OAuth machinery: the gateway binds to loopback
    for one local caller, where resource-metadata discovery and an issuer are
    pure surface area. One constant-time token compare; anything else is 401.
    An empty token means auth was explicitly disabled by the operator.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        stype = scope["type"]
        # lifespan is emitted by the ASGI server itself (startup/shutdown), never
        # attacker-reachable — it must pass without a token. Everything else
        # (http, websocket, or any future scope) is authenticated: fail closed.
        if stype != "lifespan" and self.token:
            import hmac
            auth = ""
            for k, v in scope.get("headers") or []:
                if k == b"authorization":
                    auth = v.decode("latin-1")
                    break
            if not hmac.compare_digest(auth, "Bearer " + self.token):
                if stype == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await send({
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"text/plain"),
                                    (b"www-authenticate", b"Bearer")],
                    })
                    await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


async def serve_http(
    gateway: Gateway,
    *,
    host: str = "127.0.0.1",
    port: int = 8848,
    token: str = "",
    ready_event=None,
) -> None:
    """Serve the gateway over streamable HTTP with shared-secret bearer auth.

    This is the transport the jailed-caller launcher needs: the gateway runs
    OUTSIDE the caller's OS sandbox (a stdio child would inherit the jail and
    lose its own write access), reachable only through the network hole the
    sandbox settings explicitly leave open. An empty `token` disables auth —
    only sane on loopback, and the CLI makes that an explicit loud opt-in.
    `ready_event` (an asyncio.Event) is set once the socket is accepting.
    """
    _require_mcp()
    import uvicorn

    server = _build_server(gateway)
    app = BearerAuthASGI(server.streamable_http_app(stateless_http=True), token)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    uv = uvicorn.Server(config)
    if ready_event is not None:
        async def _signal_ready():
            while not uv.started:
                import anyio
                await anyio.sleep(0.05)
            ready_event.set()
        import asyncio
        asyncio.get_running_loop().create_task(_signal_ready())
    try:
        await uv.serve()
    finally:
        gateway.close()
