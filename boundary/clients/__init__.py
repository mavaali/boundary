from boundary.clients.base import ChatResponse, Message, ModelClient, ToolCall

__all__ = ["ModelClient", "Message", "ToolCall", "ChatResponse"]


def make_client(name: str, **kwargs) -> ModelClient:
    name = name.lower()
    if name == "copilot":
        from boundary.clients.copilot import CopilotClient
        return CopilotClient(**kwargs)
    if name == "together":
        from boundary.clients.together import TogetherClient
        return TogetherClient(**kwargs)
    if name == "anthropic":
        from boundary.clients.anthropic import AnthropicClient
        return AnthropicClient(**kwargs)
    if name == "openrouter":
        from boundary.clients.openrouter import OpenRouterClient
        return OpenRouterClient(**kwargs)
    raise ValueError(f"unknown client: {name}")
