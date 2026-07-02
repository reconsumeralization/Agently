from agently import Agently

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-4.5-turbo-vl",
        "auth": "<QianFan-API-Key>",
        "request_options": {
            "temperature": 0.7,
        },
    },
).set_settings("debug", "detail")

agent = Agently.create_agent()

result = agent.image(
    question="这是什么？",
    url="https://cdn.deepseek.com/logo.png?x-image-process=image%2Fresize%2Cw_1920",
).start()

print(result)

# Expected output (requires QianFan API key):
# <model description of the DeepSeek logo image in response to "这是什么？">
# (Content is non-deterministic; the model should mention a logo or brand element.)
#
# How it works:
# .image(question="...", url="...") builds a multimodal user-turn message
# following the OpenAI Vision format.
# Use file="..." for a local image or files=[...] / urls=[...] for multi-image input.
# Any OpenAI-compatible VLM provider works here; swap base_url, model, and auth
# to switch providers.  debug="detail" prints the raw request/response stream.
