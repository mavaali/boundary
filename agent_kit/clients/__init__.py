from agent_kit.clients.base import ModelClient, Message, ToolCall, ChatResponse

__all__ = ["ModelClient", "Message", "ToolCall", "ChatResponse"]


def make_client(name: str, **kwargs) -> ModelClient:
    name = name.lower()
    if name == "copilot":
        from agent_kit.clients.copilot import CopilotClient
        return CopilotClient(**kwargs)
    if name == "together":
        from agent_kit.clients.together import TogetherClient
        return TogetherClient(**kwargs)
    if name == "anthropic":
        from agent_kit.clients.anthropic import AnthropicClient
        return AnthropicClient(**kwargs)
    raise ValueError(f"unknown client: {name}")
