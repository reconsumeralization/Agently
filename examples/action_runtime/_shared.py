from __future__ import annotations

import os
from pprint import pprint

from dotenv import find_dotenv, load_dotenv

from agently import Agently
from agently.core import Action


def configure_deepseek(*, temperature: float = 0.1):
    load_dotenv(find_dotenv())

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DEEPSEEK_API_KEY. Put it in your environment or .env before running this example.")

    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat"),
            "model_type": "chat",
            "auth": api_key,
            "request_options": {
                "temperature": temperature,
            },
        },
    )
    Agently.set_settings("debug", False)
    return Agently


def create_deepseek_agent(system_prompt: str):
    configure_deepseek()
    agent = Agently.create_agent()
    agent.set_agent_prompt("system", system_prompt)
    agent.set_action_loop(max_rounds=4)
    return agent


def print_action_results(records):
    print("[INTERMEDIATE_ACTION_RESULTS]")
    pprint(records)
    print("[INTERMEDIATE_ACTION_RESULTS_FOR_REPLY]")
    pprint(Action.to_action_results(records))


def print_response(response):
    print("[REPLY]")
    print(response.result.get_text())

    extra = response.result.full_result_data.get("extra") or {}
    action_logs = extra.get("action_logs", extra.get("tool_logs", [])) if isinstance(extra, dict) else []

    print("[ACTION_LOGS_FROM_RESPONSE_RESULT]")
    pprint(action_logs)
