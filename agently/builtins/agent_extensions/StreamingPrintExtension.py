from agently.core import BaseAgent


class StreamingPrintExtension(BaseAgent):
    async def async_streaming_print(self):
        async_generator = self.get_async_generator(type="delta")
        print()
        async for delta in async_generator:
            print(delta, end="", flush=True)
        print()

    def streaming_print(self):
        generator = self.get_generator(type="delta")
        print()
        for delta in generator:
            print(delta, end="", flush=True)
        print()
