from __future__ import annotations

import os

from agently import Agently


def configure_ollama_qwen(*, max_tokens: int, temperature: float = 0.0) -> str:
    """Configure the local OpenAI-compatible Ollama endpoint for Qwen examples."""

    model = os.getenv(
        "AGENT_PATTERN_OLLAMA_MODEL",
        os.getenv("OLLAMA_DEFAULT_MODEL", "qwen3.5:9b"),
    )
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
            "api_key": os.getenv("OLLAMA_API_KEY", "ollama-local"),
            "model": model,
            "model_type": "chat",
            "request_retry": {"max_attempts": 1, "after_output": False},
            "request_options": {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "reasoning_effort": "none",
            },
        },
    )
    Agently.set_settings("debug", False)
    return model


__all__ = ["configure_ollama_qwen"]
