"""Smoke test: ask the agent to write a hello.txt and read it back."""
from agent_kit import Agent

agent = Agent(
    name="hello",
    system_prompt=(
        "You are a coding agent. Use the provided tools to complete tasks. "
        "Always verify your work by reading files back. Be concise."
    ),
    workspace="/tmp/agent-kit-hello",
    client="copilot",
)

result = agent.run(
    "Create a file called greeting.txt containing 'hello from agent-kit'. "
    "Then read it back to confirm. Finally tell me what you did.",
    verbose=True,
)
print("\n--- FINAL ---")
print(result.final_message.content)
agent.close()
