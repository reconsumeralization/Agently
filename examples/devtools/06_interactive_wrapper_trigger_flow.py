"""InteractiveWrapper example using a TriggerFlow with streamed stage updates."""

import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData
from agently_devtools import InteractiveWrapper

from _observation_helper import register_example_observation, unregister_example_observation


bridge = register_example_observation(group_id="interactive-wrapper-triggerflow")


# Create a TriggerFlow with multiple stages
flow = TriggerFlow(name="interactive-demo-flow")


@flow.chunk
async def validate_input(data: TriggerFlowRuntimeData):
    """Validate and prepare the input."""
    value = data.value
    if isinstance(value, dict):
        message = value.get("input", "")
    else:
        message = str(value)

    if not message:
        await data.async_put_into_stream("Validation failed: empty input received.\n")
        return {"status": "error", "message": "Empty input received"}

    await data.async_put_into_stream(f"Validated input: {message}\n")
    return {"status": "validated", "message": message, "length": len(message)}


@flow.chunk
async def process_message(data: TriggerFlowRuntimeData):
    """Process the validated message."""
    payload = data.value
    if payload.get("status") == "error":
        await data.async_put_into_stream("Skipping processing because validation failed.\n")
        return payload

    message = payload.get("message", "")

    # Simple processing: convert to uppercase and add metadata
    processed = {
        "original": message,
        "uppercase": message.upper(),
        "word_count": len(message.split()),
        "status": "processing_complete",
    }

    await asyncio.sleep(0.2)
    await data.async_put_into_stream("Transforming message to uppercase...\n")
    await asyncio.sleep(0.2)
    await data.async_put_into_stream(f"Word count: {processed['word_count']}\n")
    return processed


@flow.chunk
async def finalize(data: TriggerFlowRuntimeData):
    """Finalize and format the result."""
    result = dict(data.value)
    if result.get("status") == "error":
        await data.async_put_into_stream("Flow finished with a validation error.\n")
        await data.async_stop_stream()
        return result

    result["final_status"] = "completed"
    await data.async_put_into_stream("Flow complete. Final structured result is ready.\n")
    await data.async_stop_stream()
    return result


# Connect flow stages
flow.to(validate_input).to(process_message).to(finalize).end()


# Wrap the flow with InteractiveWrapper
interactive = InteractiveWrapper(
    flow,
    title="TriggerFlow Demo",
    description="Interactive TriggerFlow that streams stage updates before returning the final structured result",
)


if __name__ == "__main__":
    print(f"Interactive UI: {interactive.ui_url}")
    print("The flow streams progress messages for validate -> process -> finalize before showing the final result.")
    print("If agently-devtools start is running, TriggerFlow runs will also appear in the local DevTools console.")
    try:
        interactive.wait()
    finally:
        unregister_example_observation(bridge)
