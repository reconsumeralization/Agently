"""Basic InteractiveWrapper example using a streaming callable."""

import time
from collections.abc import Generator

from agently_devtools import InteractiveWrapper

from _observation_helper import register_example_observation, unregister_example_observation


bridge = register_example_observation(group_id="interactive-wrapper-basic")


def echo_handler(request_data: dict, **options) -> Generator[str, None, None]:
    """
    Stream a simple echo response chunk by chunk.

    Args:
        request_data: Dictionary with 'input' key containing user message
        **options: Additional options from FastAPIHelper

    Returns:
        Incremental text chunks rendered as they arrive in the UI
    """
    message = request_data.get("input", "").strip()
    if not message:
        yield "Please provide some input."
        return

    chunks = [
        "Analyzing input...\n",
        f"Echo: {message}\n",
        f"Message length: {len(message)} characters\n",
        f"Word count: {len(message.split())}\n",
    ]
    for chunk in chunks:
        time.sleep(0.2)
        yield chunk


# Create InteractiveWrapper instance
interactive = InteractiveWrapper(
    echo_handler,
    title="Streaming Echo Demo",
    description="Generator-based echo handler that streams chunks into the interactive UI",
)


if __name__ == "__main__":
    print(f"Interactive UI: {interactive.ui_url}")
    print("This example streams several text chunks instead of returning one full response.")
    print(
        "ObservationBridge is also registered, but this plain callable example does not emit Agently runtime runs unless you replace the handler with an Agently target."
    )
    try:
        interactive.wait()
    finally:
        unregister_example_observation(bridge)
