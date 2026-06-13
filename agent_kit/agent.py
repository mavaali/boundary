from __future__ import annotations
from pathlib import Path

from agent_kit.clients import make_client
from agent_kit.clients.base import Message, ModelClient
from agent_kit.loop import LoopResult, run_loop
from agent_kit.tools.registry import ToolRegistry
from agent_kit.tools.workspace import Workspace
from agent_kit.tools.fs import register_fs_tools
from agent_kit.tools.shell import register_shell_tools
from agent_kit.tools.web import register_web_tools
from agent_kit.tools.clawpilot import register_clawpilot_tools
from agent_kit.transcript import Transcript


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
        max_iters: int = 25,
        transcript: Transcript | None | bool = True,
        client_kwargs: dict | None = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        if isinstance(client, str):
            self.client = make_client(client, **(client_kwargs or {}))
        else:
            self.client = client
        self.tools = tools or ToolRegistry()
        if enable_fs:
            register_fs_tools(self.tools, self.workspace)
        if enable_shell:
            register_shell_tools(self.tools, self.workspace, timeout=shell_timeout, allow=True)
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
