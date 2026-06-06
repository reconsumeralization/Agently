import asyncio
import time
from agently import Agently

agent = Agently.create_agent()

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
    },
)


## Different Response Results and Use Cases
def different_response_results():
    # Create a result facade once.
    # The actual request runs when you consume data from the result,
    # and the result can be reused multiple times without re-requesting.
    result = (
        agent.input("Please explain recursion with a short example.")
        .output(
            {
                "definition": (str, "Short definition"),
                "example": (str, "Simple example"),
            },
        )
        .get_result()
    )

    # 1) get_text(): plain text, best for quick chat outputs.
    text = result.get_text()
    print("[text]", text)

    # 2) get_data(): parsed structured data (when output() is used).
    data = result.get_data()
    print("[data]", data)

    # 3) get_data_object(): Pydantic model instance for strict typing.
    # Useful when you want attribute access and validation.
    data_object = result.get_data_object()
    print("[data_object]", data_object)

    # 4) get_meta(): request/response metadata (tokens, model info, etc.).
    meta = result.get_meta()
    print("[meta]", meta)

    # 5) get_generator(): streaming outputs for realtime UX or incremental parsing.
    # type can be: "delta", "specific", "instant", "streaming_parse", "original", "all"
    # "delta" yields tokens; "instant"/"streaming_parse" yields structured path updates.
    result_stream = agent.input("List 3 recursion tips.").output({"tips": [(str, "Short tip")]}).get_result()
    for item in result_stream.get_generator(type="delta"):
        print(item, end="", flush=True)
    print()


# different_response_results()


## Async Variants (same result types with async APIs)
async def async_request_support():
    result = (
        agent.input("Please explain recursion with a short example.")
        .output({"definition": (str, "Short definition"), "example": (str, "Simple example")})
        .get_result()
    )
    text = await result.async_get_text()
    data = await result.async_get_data()
    data_object = await result.async_get_data_object()
    meta = await result.async_get_meta()
    print("[async text]", text)
    print("[async data]", data)
    print("[async data_object]", data_object)
    print("[async meta]", meta)


## Async Concurrency (two requests in parallel)
async def concurrent_requests():
    start_time = time.perf_counter()

    async def ask(prompt: str):
        print("[concurrent start]:", prompt, start_time)
        result = agent.input(prompt).get_result()
        return await result.async_get_text()

    result_1, result_2 = await asyncio.gather(
        ask("Summarize recursion in one sentence."),
        ask("Give one example of recursion in Python."),
    )
    end_time = time.perf_counter()
    print("[concurrent end]", end_time)
    print("[concurrent elapsed]", end_time - start_time)
    print("[concurrent result 1]", result_1)
    print("[concurrent result 2]", result_2)


async def main():
    # await async_request_support()
    # await concurrent_requests()
    pass


asyncio.run(main())

# All functions are commented out — uncomment one to run with a local Ollama model.
# Model output is non-deterministic text; structure of meta is stable.
#
# How it works:
# get_result() returns a lazy result facade; the model request does not start until
# data is consumed.  Once consumed, all result types (text, data, data_object, meta)
# are cached on the result instance and can be read multiple times without re-requesting.
# Result accessor pairs:
#   get_text()        / async_get_text()        — raw reply as a string
#   get_data()        / async_get_data()        — parsed structured dict (requires .output())
#   get_data_object() / async_get_data_object() — Pydantic model of the structured dict
#   get_meta()        / async_get_meta()        — request metadata (model, tokens, timing, …)
#   get_generator()   / get_async_generator()   — streaming iterator; type=
#     "delta"          : raw token strings
#     "instant"        : streaming_parse nodes with .path, .delta, .wildcard_path
#     "specific"       : (event_name, data) tuples (delta, reasoning_delta, tool_calls)
# concurrent_requests() shows that multiple async_get_text() calls on separate result
# handles run in parallel via asyncio.gather(), each making its own independent request.
