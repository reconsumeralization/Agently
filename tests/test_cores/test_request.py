import os
import base64
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

import pytest
import asyncio
from asyncio import Task
from contextlib import suppress
from typing import AsyncGenerator, cast

from agently import Agently
from agently.core.model.AttachmentInput import build_image_attachment
from agently.core.model.ModelRequest import ModelRequest
from agently.utils import SerializableStateDataNamespace
from agently.builtins.plugins.ModelRequester.OpenAICompatible import (
    ModelRequesterSettings,
)

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def configure_ollama(request_settings: ModelRequesterSettings):
    request_settings["base_url"] = OLLAMA_BASE_URL
    request_settings["model"] = OLLAMA_MODEL
    request_settings["model_type"] = "chat"
    request_settings["auth"] = None


def test_image_builds_local_and_remote_attachment(tmp_path):
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(PNG_BYTES)

    attachment = build_image_attachment(
        question="Compare these images.",
        file=image_path,
        urls=["https://example.com/remote.png"],
        detail="high",
    )

    assert attachment[0] == {"type": "text", "text": "Compare these images."}
    assert attachment[1]["type"] == "image_url"
    assert attachment[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert attachment[1]["image_url"]["detail"] == "high"
    assert attachment[2] == {
        "type": "image_url",
        "image_url": {
            "url": "https://example.com/remote.png",
            "detail": "high",
        },
    }


def test_request_image_sets_rich_content_attachment(tmp_path):
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(PNG_BYTES)

    request = Agently.create_request()
    request.image(question="What is in the image?", files=[image_path], url="https://example.com/second.png")

    messages = request.prompt.to_messages(rich_content=True)
    content = messages[0]["content"]
    assert content[0] == {"type": "text", "text": "What is in the image?"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2]["image_url"]["url"] == "https://example.com/second.png"


def test_agent_image_supports_always_prompt(tmp_path):
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(PNG_BYTES)

    agent = Agently.create_agent()
    agent.image(question="Describe this persistent image.", file=image_path, always=True)

    messages = agent.agent_prompt.to_messages(rich_content=True)
    content = messages[0]["content"]
    assert content[0] == {"type": "text", "text": "Describe this persistent image."}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_image_rejects_missing_sources():
    with pytest.raises(ValueError, match="requires at least one image source"):
        build_image_attachment(question="What is shown?")


def test_image_rejects_non_image_file(tmp_path):
    text_path = tmp_path / "notes.txt"
    text_path.write_text("not an image")

    with pytest.raises(ValueError, match="only supports image files"):
        build_image_attachment(question="What is shown?", file=text_path)


@pytest.mark.asyncio
async def test_single_request(require_ollama):
    request = ModelRequest(
        Agently.plugin_manager,
        parent_settings=Agently.settings,
    )
    request_settings = cast(
        ModelRequesterSettings,
        SerializableStateDataNamespace(
            Agently.settings,
            "plugins.ModelRequester.OpenAICompatible",
        ),
    )
    configure_ollama(request_settings)

    request.prompt["input"] = "你是谁"

    async for delta in request.get_async_generator(type="delta"):
        print(delta)


@pytest.mark.asyncio
async def test_multiple_responses_independent_consumption(require_ollama):
    request = ModelRequest(
        Agently.plugin_manager,
        parent_settings=Agently.settings,
    )
    request_settings = cast(
        ModelRequesterSettings,
        SerializableStateDataNamespace(
            Agently.settings,
            "plugins.ModelRequester.OpenAICompatible",
        ),
    )

    configure_ollama(request_settings)

    prompts = ["Hello, how are you?", "Hello again!", "Who are you?"]
    responses = []

    for prompt_text in prompts:
        request.prompt.set("input", prompt_text)
        request.prompt.set(
            "output",
            {
                "thinking": (str,),
                "reply": ([str],),
            },
        )
        responses.append(request.get_response())

    async def consume_response(response):
        async for data in response.get_async_generator(content="delta"):
            print(f"[{id(response)}]: {data}")

    tasks: list[Task] = [asyncio.create_task(consume_response(resp)) for resp in responses]

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
