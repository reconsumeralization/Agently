import asyncio
from typing import Any, Callable, cast

from agently import TriggerFlow, TriggerFlowRuntimeData


class SimpleLogger:
    def info(self, message: str):
        print(f"[logger] {message}")


def search_tool(query: str):
    return [
        {"title": f"{query} - result 1"},
        {"title": f"{query} - result 2"},
    ]


async def triggerflow_runtime_resources_demo():
    flow = TriggerFlow(name="step-17-runtime-resources")
    flow.update_runtime_resources(logger=SimpleLogger())

    async def prepare(data: TriggerFlowRuntimeData):
        query = str(data.input).strip()
        await data.async_set_state("request", {"query": query})
        cast(SimpleLogger, data.require_resource("logger")).info(f"prepared: {query}")
        return await data.async_pause_for(
            type="human_input",
            payload={"question": f"search news for '{query}'?"},
            resume_event="UserFeedback",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        request = data.get_state("request") or {}
        logger = cast(SimpleLogger, data.require_resource("logger"))
        search = cast(Callable[[str], list[dict[str, Any]]], data.require_resource("search_tool"))
        results = search(str(request.get("query") or ""))
        logger.info(f"searched {len(results)} items")
        await data.async_set_state(
            "final",
            {
                "request": request,
                "feedback": data.input,
                "results": results,
            },
        )

    flow.to(prepare)
    flow.when("UserFeedback").to(finalize)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("AI chips")
    saved_state = execution.save()

    restored = flow.create_execution(
        auto_close=False,
        runtime_resources={"search_tool": search_tool},
    )
    restored.load(saved_state)

    interrupt_id = next(iter(restored.get_pending_interrupts()))
    await restored.async_continue_with(interrupt_id, {"approved": True})
    state = await restored.async_close()
    assert len(state["final"]["results"]) == 2
    print(state["final"])


if __name__ == "__main__":
    asyncio.run(triggerflow_runtime_resources_demo())
