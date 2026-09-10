"""Generate a JSON root string with a real local Qwen model.

RootModel[str] describes the carrier, not a long-text mode. The model returns
one JSON string; data returns its decoded text and the typed reader retains
the original RootModel. Braces in prose do not introduce another JSON root.

Configure QWEN_BASE_URL, QWEN_MODEL and QWEN_API_KEY to override local defaults.

Expected key output from local qwen3.8:27b-mlx on 2026-09-10:
{"text": "{user} 是一个由调用方提供的用户名占位符。", "same_typed_value": true}
Wording varies. Three observed runs each needed one ordinary validation retry:
the first response was not a complete JSON string and was rejected, not repaired
silently. Reusing the typed reader did not cause another model request.
This demonstrates root parsing/validation, not natural length continuation.
"""

from __future__ import annotations

import asyncio
import json
import os

from pydantic import RootModel

from agently import Agently


async def main() -> None:
    Agently.set_settings("OpenAICompatible", {
        "base_url": os.getenv("QWEN_BASE_URL", "http://127.0.0.1:11434/v1"),
        "api_key": os.getenv("QWEN_API_KEY", "ollama-local"),
        "model": os.getenv("QWEN_MODEL", "qwen3.8:27b-mlx"),
        "model_type": "chat",
        "request_retry": {"max_attempts": 1, "after_output": False},
        "request_options": {"temperature": 0.3, "reasoning_effort": "none"},
    })
    result = (
        Agently.create_agent("root-output").create_request()
        .input({"notation": "{user}", "meaning": "由调用方提供的用户名占位符"})
        .instruct("用一句中文说明 [input.notation] 的含义，保留原符号。")
        .output(RootModel[str], format="json")
        .get_result()
    )
    print("Raw JSON:", await result.async_get_text())
    text = await result.async_get_data(max_retries=1)
    typed = await result.async_get_data_object(max_retries=1)
    print(json.dumps({"text": text, "same_typed_value": isinstance(typed, RootModel)
        and typed.root == text}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
