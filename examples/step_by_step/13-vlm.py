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
    question="What's this?",
    url="https://cdn.deepseek.com/logo.png?x-image-process=image%2Fresize%2Cw_1920",
).start()

print(result)

# Runs on import — requires a valid QianFan API key in the auth field.
# Expected output: model description of the DeepSeek logo image (blue stylized "D" mark).
# Content is non-deterministic; the model should mention a logo or branding element.
#
# How it works:
# .image(question="...", url="...") builds the OpenAI-style multimodal content
# list for you: one text part plus one image_url part.
# Use file="..." for a local image or files=[...] / urls=[...] for multi-image input.
# Any OpenAI-compatible VLM can be used here; swap base_url, model, and auth
# to use a different provider (Ollama, OpenAI, DeepSeek-VL, etc.).
# debug="detail" prints the raw request/response stream to console.
