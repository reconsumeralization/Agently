---
title: Model Response
description: Reading text, structured data, metadata, and streaming events from one result.
keywords: Agently, result, get_result, get_data, get_text, get_meta, generator, streaming
---

# Model Result

> Languages: **English** · [中文](../../cn/requests/model-response.md)

`agent.input(...).start()` is a convenience that runs the request and returns the parsed dict. For everything more interesting — text, metadata, streaming, reuse — go through `get_result()`.

## Two consumption styles

```python
# Style A: one shot, return parsed data immediately
result = agent.input("...").output({...}).start()

# Style B: hold a reusable result facade
result = agent.input("...").output({...}).get_result()
text = result.get_text()
data = result.get_data()
meta = result.get_meta()
```

Style B is the default for non-trivial code. The actual model call runs lazily when you first consume from `result`, then results are **cached** — multiple reads do not re-issue the request. `get_response()` remains a compatibility alias for older code and returns the same result facade.

## Reader methods

| Method | Returns |
|---|---|
| `result.get_text()` | full plain text |
| `result.get_data()` | parsed structured dict (when `output()` was used) |
| `result.get_data_object()` | Pydantic instance (when `output()` was given a `BaseModel`) |
| `result.get_meta()` | dict of usage / model info / timing |

Each has an async sibling: `async_get_text()`, `async_get_data()`, `async_get_data_object()`, `async_get_meta()`.

Mixing readers is fine — they all consume from the same cached result:

```python
result = agent.input("...").output({...}).get_result()
data = result.get_data()        # triggers the request
text = result.get_text()        # already cached
meta = result.get_meta()        # already cached
```

This is also how `.validate(...)` runs only once per result — the cached result is what gets validated.

## Streaming

`result.get_generator(type=...)` (sync) and `get_async_generator(type=...)` (async) yield streaming events. The `type` parameter selects what you see:

| `type` | What you get | Use it for |
|---|---|---|
| `"delta"` | raw token deltas | terminal-style typing UX |
| `"instant"` | structured `StreamingData` events with `path`, `delta`, `value`, and `is_completed` | field-level UI updates |
| `"streaming_parse"` | alias for the same structured streaming parser used by `instant` | compatibility / incremental dict reads |
| `"specific"` | `(event, data)` tuples filtered by event type (`delta`, `reasoning_delta`, `tool_calls`, etc.) | pick exactly the events you care about |
| `"original"` | raw provider events | debugging / passthrough |
| `"all"` | every event with type tag | exhaustive logging |

For common type annotations, import the public stream item types from
`agently`: `StreamingData` for `instant` / `streaming_parse`,
`AgentlySpecificResultMessage` for `specific`, and
`AgentlyModelResultMessage` for `all`. The same types remain available from
`agently.types.data` when you want the full typed data namespace.
The older `AgentlySpecificResponseMessage`, `AgentlyModelResponseMessage`, and
related `Response` aliases remain available from `agently.types.data` for
compatibility, but they are not re-exported from the `agently` root. The
`Result` names are the recommended API.

### Delta example

```python
gen = agent.input("Tell me a recursion story.").get_generator(type="delta")
for delta in gen:
    print(delta, end="", flush=True)
```

### Instant example (structured)

```python
gen = (
    agent.input("Give me a definition and three tips.")
    .output({
        "definition": (str, "Definition", True),
        "tips": [(str, "Tip", True)],
    })
    .get_generator(type="instant")
)
for item in gen:
    if item.delta:
        print(f"[{item.path}] + {item.delta}")
    if item.is_completed:
        print(f"[{item.path}] done")
```

`item` exposes `.path` (e.g. `"tips[0]"`), `.wildcard_path` (`"tips[*]"`),
`.value`, `.delta`, `.is_completed`, and `.event_type`. Use `.delta` to update
the visible field while it is growing. Use `.is_completed` / `event_type=="done"`
when downstream work should wait until the field is closed.
`.is_complete` remains a compatibility alias for stream events, but it is
deprecated and will be removed in Agently 4.2.

### High-value pattern: stream fields to UI, then read the durable result

Use `instant` when the application can show or route individual structured
fields before the whole answer is finished. The stream is for progressive UI
state; the final business object should still come from `async_get_data()`.

```python
import asyncio
from collections import defaultdict
from agently import Agently

agent = Agently.create_agent()


async def stream_triage_card(ticket_text: str):
    result = (
        agent
        .input(ticket_text)
        .output(
            {
                "status_summary": (str, "One sentence status for the user", True),
                "risk_flags": [(str, "Concrete risk flag", True)],
                "next_actions": [(str, "Action the support team should take", True)],
                "customer_reply": (str, "Polished reply to the customer", True),
            },
            format="json",
        )
        .get_result()
    )

    ui_state: dict[str, str] = defaultdict(str)

    async for item in result.get_async_generator(type="instant"):
        if item.delta:
            # Render a field-level patch to your UI / SSE / WebSocket channel.
            ui_state[item.path] += item.delta
            print({"path": item.path, "delta": item.delta})
        if item.is_completed:
            print({"path": item.path, "status": "done", "value": item.value})

    # No second request: this reads the cached final parse from the same result.
    final_data = await result.async_get_data()
    return final_data


asyncio.run(stream_triage_card(
    "Ticket T-104: enterprise billing export failed twice; CFO waiting."
))
```

Prefer async consumption for services. Synchronous `get_generator(type="instant")`
is fine for scripts and notebooks.

### Specific example (events)

```python
gen = agent.input("Hello.").get_generator(type="specific")
for event, data in gen:
    if event == "delta":
        print(data, end="", flush=True)
    elif event == "reasoning_delta":
        print("[reasoning]", data, end="", flush=True)
    elif event == "tool_calls":
        print("[tool call]", data)
```

### Reasoning events

Some providers expose reasoning in native response fields. Some local or
OpenAI-compatible reasoning models may instead place a leading outer
`<think>...</think>` block in ordinary content. Agently normalizes both cases
before structured parsing:

- `reasoning_delta` / `reasoning_done` carry reasoning text.
- `delta` / `done` carry only the answer payload that parsers should consume.
- `original_delta` / `original_done` keep the provider's raw content unchanged.
- Only a complete leading outer `<think>...</think>` before the answer payload is
  normalized. `<think>` inside a field, code block, or long text payload remains
  ordinary answer content.

## Async streaming

Same generators in async form:

```python
import asyncio

async def main():
    result = agent.input("...").output({...}).get_result()
    async for item in result.get_async_generator(type="instant"):
        if item.is_completed:
            print(item.path, item.value)

asyncio.run(main())
```

For services and TriggerFlow usage, async is the recommended path — see [Async First](../start/async-first.md).

## Concurrency

Because `get_result()` only kicks off the actual request when you consume it, you can build many results up front and consume them in parallel:

```python
import asyncio

async def ask(prompt):
    r = agent.input(prompt).get_result()
    return await r.async_get_text()

results = await asyncio.gather(
    ask("Summarize recursion."),
    ask("Give one Python example."),
)
```

This is a standard async pattern; nothing in Agently is special about it.

## Don't re-issue when you can re-read

```python
# bad — runs the request three times
text = agent.input("...").start()
data = agent.input("...").output({...}).start()
meta = agent.input("...").output({...}).get_result().get_meta()

# good — runs once, reads three views
result = agent.input("...").output({...}).get_result()
text = result.get_text()
data = result.get_data()
meta = result.get_meta()
```

## See also

- [Async First](../start/async-first.md) — when to switch to `get_async_generator(...)`
- [Output Control](output-control.md) — what runs between "model returned" and "you read"
- [Schema as Prompt](schema-as-prompt.md) — what `output()` accepts
