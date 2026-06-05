from agently import Agently

agent = Agently.create_agent()

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
    },
)


## Streaming Output Basics
def basic_delta_streaming():
    # Agently provides delta streaming to show partial output early,
    # so users don't stare at a blank screen.
    gen = agent.input("Give me a short speech about recursion.").get_generator(type="delta")
    for delta in gen:
        print(delta, end="", flush=True)
    print()


# basic_delta_streaming()


## Instant / Streaming-Parse for Structured Output
def instant_structured_streaming():
    # Instant streaming emits structured StreamingData patches while the
    # response is still generating. It is useful for dashboards, SSE/WebSocket
    # UIs, long reports, and workflow panels that can render one field before
    # the full structured result is ready.
    gen = (
        agent.input(
            "Turn this support note into a customer-safe update: "
            "enterprise billing export failed twice; CFO is waiting."
        )
        .output(
            {
                "status_summary": (str, "One sentence status for a support dashboard", True),
                "risk_flags": [(str, "Concrete risk flag", True)],
                "next_actions": [(str, "Support team action", True)],
                "customer_reply": (str, "Polished reply to the customer", True),
            },
            format="json",
        )
        .get_generator(type="instant")
    )

    field_buffers: dict[str, str] = {}
    for data in gen:
        if data.delta:
            field_buffers[data.path] = field_buffers.get(data.path, "") + data.delta
            print(f"[patch] {data.path}: +{data.delta!r}")
        if data.is_completed:
            print(f"[done]  {data.path}: {data.value!r}")
    print()


# instant_structured_streaming()


## Specific Event Streaming
def specific_event_streaming():
    # Agently provides specific-event streaming so you can pick only the events you care about
    # (delta / tool_calls / reasoning).
    gen = agent.input("Tell me a short story about recursion.").get_generator(type="specific")
    current_event = None
    for event, data in gen:
        if event in ("reasoning_delta", "delta"):
            if current_event != event:
                current_event = event
                label = "reasoning" if event == "reasoning_delta" else "answer"
                print(f"\n[{label}] ", end="", flush=True)
            print(data, end="", flush=True)
        elif event == "tool_calls":
            print("\n[tool_calls]", data)
    print()


specific_event_streaming()


## Async Variants
async def async_streaming():
    # Agently also provides async streaming for higher concurrency workloads.
    gen = agent.input("Give three recursion tips.").get_async_generator(type="delta")
    async for delta in gen:
        print(delta, end="", flush=True)
    print()


# async def main():
#     await async_streaming()

# specific_event_streaming() runs on import (not commented out); it streams a story.
# Expected output shape (content is variable):
#   [reasoning]  <thinking tokens if the model supports it, otherwise absent>
#   [answer]  <story text tokens streamed live>
#
# How it works:
# Three streaming modes are demonstrated:
#
# 1. type="delta"   — yields raw token strings; simplest for chat UIs that just need text.
#
# 2. type="instant" — yields structured streaming_parse nodes, each with:
#      .path          : dotted key path of the currently streaming field ("definition", "tips[0]", …)
#      .wildcard_path : path with array indices replaced by * ("tips[*]")
#      .delta         : the latest token fragment at this path
#      .value         : parser's current value at this path
#      .is_completed   : True when this field/path is closed
#    Use wildcard_path to dispatch rendering per field type without hard-coding
#    indices. Treat instant events as provisional UI state; read get_data() /
#    async_get_data() at the end for durable business state.
#
# 3. type="specific" — yields (event, data) tuples; recognized event names:
#      "delta"           : text token
#      "reasoning_delta" : thinking/reasoning token (models that expose chain-of-thought)
#      "tool_calls"      : tool-call payload
#    Only events you actually handle need to be wired up; others can be skipped.
