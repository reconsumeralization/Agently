import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="basic-flow")


async def say_hello(data):
    greeting = f"Hello, {data.input}"
    await data.async_set_state("greeting", greeting)
    return data.input


async def say_bye(data):
    farewell = f"Bye, {data.input}"
    await data.async_set_state("farewell", farewell)
    return data.input


flow.to(say_hello).to(say_bye)


async def main():
    execution = flow.create_execution()
    await execution.async_start("Agently")
    state = await execution.async_close()
    assert state["greeting"] == "Hello, Agently"
    assert state["farewell"] == "Bye, Agently"
    print(state)


asyncio.run(main())
