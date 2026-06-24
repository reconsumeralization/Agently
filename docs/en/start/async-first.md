---
title: Async First
description: When async is the default path, and which APIs to reach for.
keywords: Agently, async, async_get, get_async_generator, async_start
---

# Async First

Agently is async-native at the runtime layer. Sync methods are convenience wrappers generated from the async ones via `FunctionShifter.syncify()`. For real services, async should be the default path.

## When sync is fine

- One-off scripts, notebooks, teaching demos.
- Code that doesn't share an event loop with anything else.

## When async is the default

- Inside FastAPI, ASGI workers, SSE / WebSocket handlers, or any code already running in `asyncio`.
- Streaming UI where you want to react to field deltas instead of waiting for the full response.
- Combining model output with TriggerFlow events, runtime stream, or external pubsub.

## Recommended pairing

The combination worth learning first:

- `result.get_async_generator(type="instant")` — yields structured `StreamingData` patches with `path`, `delta`, `value`, and `is_complete`.
- `data.async_emit(...)` — turns nodes into TriggerFlow signals.
- `data.async_put_into_stream(...)` — forwards intermediate state to UI / SSE / logs.

`instant` events are field-level, not raw provider tokens. They can carry partial
field text in `.delta` while the field is still growing, then emit a completion
event when `.is_complete` becomes true. Treat these events as progressive UI
state; read `async_get_data()` at the end for the durable parsed object.
Annotate these stream handlers with `StreamingData` from `agently` for the
common import path, or from `agently.types.data` when you prefer the full typed
data namespace.

## API surface map

| Sync | Async equivalent |
|---|---|
| `agent.start()` / `request.start()` | `agent.async_start()` / `request.async_start()` |
| `result.get_data()` | `result.async_get_data()` |
| `result.get_text()` | `result.async_get_text()` |
| `result.get_meta()` | `result.async_get_meta()` |
| `result.get_generator(type=...)` | `result.get_async_generator(type=...)` |
| `flow.start()` | `flow.async_start()` |
| `execution.start()` / `execution.close()` | `execution.async_start()` / `execution.async_close()` |
| `data.set_state(...)` / `data.emit(...)` | `data.async_set_state(...)` / `data.async_emit(...)` |

## Minimal async example

```python
import asyncio
from agently import Agently

agent = Agently.create_agent()


async def main():
    result = (
        agent
        .input("Give me a title and two bullets.")
        .output({
            "title": (str, "Title", True),
            "items": [(str, "Bullet point", True)],
        })
        .get_result()
    )

    async for item in result.get_async_generator(type="instant"):
        if item.delta:
            print(item.path, "+", item.delta)
        if item.is_complete:
            print(item.path, "done")

    final = await result.async_get_data()
    print(final)


asyncio.run(main())
```

`get_result()` returns a reusable `ModelRequestResult`. You can pull text, structured data, and metadata from the same result without re-issuing the request — see [Model Result](../requests/model-response.md).

## Async + TriggerFlow

For event-driven orchestration, prefer:

- `flow.async_start(...)` for hidden execution sugar (returns the close snapshot).
- `flow.async_start_execution(...)` for explicit, long-lived executions you want to control yourself.
- `data.async_emit(...)` and `data.async_put_into_stream(...)` inside chunks.

See [TriggerFlow Lifecycle](../triggerflow/lifecycle.md).

## Don't oversell async

Async First improves concurrency, service composition, and progressive UX. It does **not** make a single isolated model request faster. The wall-clock latency of one request is bounded by the model, not by sync vs async.
