import asyncio
import os

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData


OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "ollama")


def configure_local_ollama():
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": OLLAMA_BASE_URL,
            "api_key": OLLAMA_API_KEY,
            "model": OLLAMA_MODEL,
            "model_type": "chat",
            "request_options": {"temperature": 0},
        },
    )


async def triggerflow_runtime_stream_demo():
    flow = TriggerFlow(name="step-10-runtime-stream")

    async def stream_steps(data: TriggerFlowRuntimeData):
        for step in range(3):
            await data.async_put_into_stream({"step": step + 1, "status": "working"})
            await asyncio.sleep(0.01)
        await data.async_set_state("done", True)

    flow.to(stream_steps)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("start")
    close_task = asyncio.create_task(execution.async_close())
    events = [event async for event in execution.get_async_runtime_stream(timeout=None)]
    state = await close_task
    assert state["done"] is True
    print(events)


async def triggerflow_agent_stream_demo():
    configure_local_ollama()
    flow = TriggerFlow(name="step-10-agent-stream")

    async def stream_reply(data: TriggerFlowRuntimeData):
        agent = Agently.create_agent()
        agent.role("Reply in one short sentence.", always=True)
        response = agent.input(str(data.input)).get_response()
        async for delta in response.get_async_generator(type="delta"):
            if delta:
                await data.async_put_into_stream({"event": "delta", "content": delta})
        final_reply = await response.async_get_text()
        await data.async_put_into_stream({"event": "final", "content": final_reply})
        await data.async_set_state("reply", final_reply)

    flow.to(stream_reply)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("Explain TriggerFlow in one sentence.")
    close_task = asyncio.create_task(execution.async_close())
    events = [event async for event in execution.get_async_runtime_stream(timeout=None)]
    state = await close_task
    assert state["reply"]
    print(events[-1])


async def main():
    await triggerflow_runtime_stream_demo()
    await triggerflow_agent_stream_demo()


if __name__ == "__main__":
    asyncio.run(main())
