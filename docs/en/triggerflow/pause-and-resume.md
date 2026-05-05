---
title: Pause and Resume
description: Suspending a chunk for human input or external events with pause_for, continuing with continue_with.
keywords: Agently, TriggerFlow, pause_for, continue_with, interrupt, human-in-the-loop
---

# Pause and Resume

> Languages: **English** · [中文](../../cn/triggerflow/pause-and-resume.md)

`pause_for(...)` lets a chunk suspend itself, returning control to the framework while it waits for an external event. The execution stays alive but idle. Auto-close is paused while a `pause_for` is outstanding. When the outside calls `continue_with(...)` (or the configured `resume_event` arrives via `emit`), the chunk wakes up with the supplied payload as its result.

## Suspend with pause_for

```python
async def ask(data: TriggerFlowRuntimeData):
    return await data.async_pause_for(
        type="human_input",
        payload={"question": f"Approve action for {data.input}?"},
        resume_event="ApprovalGiven",
    )
```

What `pause_for` does:

- Records an interrupt with a unique id.
- Stops the auto-close timer for this execution.
- Returns to the framework. The chunk's coroutine is suspended.
- The interrupt is exposed via `execution.get_pending_interrupts()`.
- When `continue_with(interrupt_id, payload)` is called (or an `emit(resume_event, payload)` matches), the awaited call returns the payload.

| Argument | Meaning |
|---|---|
| `type=` | a string label (e.g. `"human_input"`, `"approval"`, `"webhook"`). The application uses this to decide how to surface the interrupt. |
| `payload=` | structured details for whatever's responsible for resuming (UI to render a question, webhook recipient, etc.). |
| `resume_event=` | optional. If set, an `emit` of this event also resumes the pause (in addition to direct `continue_with`). |
| `interrupt_id=` | optional. Specify the id yourself; otherwise the framework generates one. |

## Resume with continue_with

```python
interrupt_id = next(iter(execution.get_pending_interrupts()))
await execution.async_continue_with(interrupt_id, {"approved": True})
```

The payload becomes the return value of the suspended `await data.async_pause_for(...)` call. The chunk continues from there.

If you specified `resume_event="ApprovalGiven"`, this also works:

```python
await execution.async_emit("ApprovalGiven", {"approved": True})
```

The first matching interrupt is resumed.

## Worked example

```python
import asyncio
from agently import TriggerFlow, TriggerFlowRuntimeData


async def main():
    flow = TriggerFlow(name="approval")

    async def ask(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            payload={"question": f"Approve refund for ticket {data.input}?"},
            resume_event="ApprovalGiven",
        )

    async def commit(data: TriggerFlowRuntimeData):
        await data.async_set_state("decision", data.input)

    flow.to(ask).to(commit)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("T-001")

    # In a real system the UI / webhook would call continue_with later.
    # Here we resume from the same coroutine for demo purposes.
    interrupt_id = next(iter(execution.get_pending_interrupts()))
    await execution.async_continue_with(interrupt_id, {"approved": True})

    snapshot = await execution.async_close()
    print(snapshot["decision"])  # {'approved': True}


asyncio.run(main())
```

Note: this flow uses `pause_for(...)`. It must be created with `flow.create_execution(...)` (or `flow.start_execution(...)`), **not** `flow.start(...)` — the hidden execution sugar has no handle the outside can use to call `continue_with`.

## Pause across process restarts

`pause_for(...)` integrates cleanly with `save` / `load`:

```python
execution = flow.create_execution(auto_close=False)
await execution.async_start("topic")
# at this point pause_for has been hit; an interrupt is pending

saved = execution.save()
# persist saved somewhere

# later, in a different process / worker:
restored = flow.create_execution(
    auto_close=False,
    runtime_resources={...},   # re-inject whatever the chunk needs
)
restored.load(saved)
interrupt_id = next(iter(restored.get_pending_interrupts()))
await restored.async_continue_with(interrupt_id, {"approved": True})
snapshot = await restored.async_close()
```

The interrupt is part of the saved state, so the new process knows what's pending. See [Persistence and Blueprint](persistence-and-blueprint.md).

## Multiple concurrent pauses

A single execution can have several outstanding interrupts (e.g., two parallel branches each waiting for human input). `get_pending_interrupts()` returns all of them; `continue_with(id, payload)` resolves one at a time.

If you want a specific id, supply `interrupt_id="my-id"` to `pause_for(...)` and use the same id in `continue_with`.

## Pause vs emit

| Pattern | Use |
|---|---|
| `pause_for` + `continue_with` | the chunk needs to **return** with the payload and resume from there |
| `emit` + `when(...)` | a separate handler should run when an event happens; the original chunk doesn't need to wait |

Pause is the right choice for human-in-the-loop because the chunk's logic depends on the human response. Emit/when is the right choice for fan-out side effects.

## auto_close interaction

`auto_close=True` does not fire while any `pause_for` is outstanding. Once `continue_with` resolves the last pending interrupt and the execution becomes idle again, the auto-close timer restarts from zero.

If you want the execution to never auto-close while waiting indefinitely, use `auto_close_timeout=None` (and remember to call `close()` explicitly).

## See also

- [Lifecycle](lifecycle.md) — when seal/close run after a resume
- [Persistence and Blueprint](persistence-and-blueprint.md) — saving across pauses
- [State and Resources](state-and-resources.md) — re-inject `runtime_resources` after `load()`
