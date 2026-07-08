import os

from dotenv import find_dotenv, load_dotenv

from agently import Agently

load_dotenv(find_dotenv())


def configure_vlm():
    api_key = os.getenv("QIANFAN_API_KEY")
    if not api_key:
        raise RuntimeError("Missing QIANFAN_API_KEY. Put it in your environment or .env before running this example.")

    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv("QIANFAN_BASE_URL", "https://qianfan.baidubce.com/v2"),
            "model": os.getenv("QIANFAN_VLM_MODEL", "ernie-4.5-turbo-vl"),
            "auth": api_key,
            "request_options": {
                "temperature": 0.7,
            },
        },
    ).set_settings("debug", "detail")


def main():
    configure_vlm()
    agent = Agently.create_agent()
    result = agent.image(
        question="这是什么？",
        url="https://cdn.deepseek.com/logo.png?x-image-process=image%2Fresize%2Cw_1920",
    ).start()
    print(result)


if __name__ == "__main__":
    main()


# Expected output (requires QIANFAN_API_KEY):
# <model description of the DeepSeek logo image in response to "这是什么？">
# (Content is non-deterministic; the model should mention a logo or brand element.)
#
# How it works:
# .image(question="...", url="...") builds a multimodal user-turn message
# following the OpenAI Vision format.
# Use file="..." for a local image or files=[...] / urls=[...] for multi-image input.
# Any OpenAI-compatible VLM provider works here; set QIANFAN_BASE_URL,
# QIANFAN_VLM_MODEL, and QIANFAN_API_KEY or adapt those env vars to switch
# providers. debug="detail" prints the raw request/response stream.
