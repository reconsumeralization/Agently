"""InteractiveWrapper example using an Agently Agent with streamed UI output."""

from agently import Agent
from agently_devtools import InteractiveWrapper

from _observation_helper import register_example_observation, unregister_example_observation


bridge = register_example_observation(group_id="interactive-wrapper-agent")


# Create a simple Agent
agent = Agent()

# Configure the agent with a system prompt
agent.set_general_instruction(
    "You are a helpful assistant. Answer user questions concisely and clearly. "
    "When the user asks 'hello', respond with a friendly greeting. "
    "Prefer short paragraphs so streamed output is easy to follow."
)

# Create InteractiveWrapper wrapping the agent
interactive = InteractiveWrapper(
    agent,
    title="Streaming Chat Agent",
    description="An interactive Agently Agent whose response appears incrementally when the configured model supports streaming",
)


if __name__ == "__main__":
    print(f"Interactive UI: {interactive.ui_url}")
    print(
        "If your configured model backend supports streaming, the answer will appear incrementally in the browser UI."
    )
    print("If agently-devtools start is running, Agent runs will also appear in the local DevTools console.")
    try:
        interactive.wait()
    finally:
        unregister_example_observation(bridge)
