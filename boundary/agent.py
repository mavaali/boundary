from __future__ import annotations

from pathlib import Path

from boundary.clients import make_client
from boundary.clients.base import Message, ModelClient
from boundary.loop import LoopResult, run_loop
from boundary.tools.clawpilot import register_clawpilot_tools
from boundary.tools.fs import register_fs_tools
from boundary.tools.registry import ToolRegistry
from boundary.tools.sandbox import resolve_auto_driver, warn_once
from boundary.tools.shell import register_shell_tools
from boundary.tools.web import register_web_tools
from boundary.tools.workspace import Workspace
from boundary.transcript import Transcript


class Agent:
    def __init__(
        self,
        name: str,
        system_prompt: str,
        workspace: str | Path | Workspace,
        client: ModelClient | str = "copilot",
        tools: ToolRegistry | None = None,
        enable_fs: bool = True,
        enable_shell: bool = True,
        enable_web: bool = False,
        enable_clawpilot: bool = False,
        shell_timeout: int = 60,
        sandbox_driver: str = "auto",
        egress_allowlist: list[str] | None = None,
        max_iters: int = 25,
        transcript: Transcript | None | bool = True,
        client_kwargs: dict | None = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        # Resolve "auto" to a concrete driver up front so the transcript and the
        # Third Umpire's egress check see the driver that actually ran, not "auto".
        if sandbox_driver == "auto":
            resolved, warning = resolve_auto_driver()
            if warning:
                warn_once(warning)
            # resolved is None only when no sandbox exists (e.g. Linux without
            # srt). Keep "auto" rather than failing construction — an agent with
            # enable_shell=False is still valid; run_sandboxed surfaces the hard
            # error if (and only if) bash is actually invoked.
            sandbox_driver = resolved or "auto"
        self.sandbox_driver = sandbox_driver
        self.egress_allowlist = list(egress_allowlist or [])
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        if isinstance(client, str):
            self.client = make_client(client, **(client_kwargs or {}))
        else:
            self.client = client
        self.tools = tools or ToolRegistry()
        if enable_fs:
            register_fs_tools(self.tools, self.workspace)
        if enable_shell:
            register_shell_tools(
                self.tools, self.workspace, timeout=shell_timeout, allow=True,
                driver=sandbox_driver, egress_allowlist=egress_allowlist,
            )
        if enable_web:
            register_web_tools(self.tools)
        if enable_clawpilot:
            register_clawpilot_tools(self.tools, workspace_root=self.workspace.root)
        self.max_iters = max_iters
        if transcript is True:
            self.transcript: Transcript | None = Transcript(agent_name=name)
        elif transcript is False:
            self.transcript = None
        else:
            self.transcript = transcript

    def run(self, task: str, verbose: bool = False, **chat_kwargs) -> LoopResult:
        messages = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=task),
        ]
        if self.transcript:
            self.transcript.log("start", agent=self.name, task=task, workspace=str(self.workspace.root))
        result = run_loop(
            self.client,
            messages,
            self.tools,
            max_iters=self.max_iters,
            transcript=self.transcript,
            verbose=verbose,
            **chat_kwargs,
        )
        if self.transcript:
            self.transcript.log("end", iterations=result.iterations, stop_reason=result.stop_reason)
        return result

    def close(self):
        if self.transcript:
            self.transcript.close()
