import os
from pprint import pprint
from typing import Literal, cast

from dotenv import find_dotenv, load_dotenv

from agently import Agently
from agently.builtins.actions import Browse, Search

SearchBackend = Literal[
    "auto", "all", "bing", "brave", "duckduckgo", "google", "grokipedia", "mojeek", "startpage", "wikipedia",
    "yahoo", "yandex",
]
SearchRegion = Literal[
    "xa-ar", "xa-en", "ar-es", "au-en", "at-de", "be-fr", "be-nl", "br-pt", "bg-bg", "ca-en", "ca-fr",
    "ct-ca", "cl-es", "cn-zh", "co-es", "hr-hr", "cz-cs", "dk-da", "ee-et", "fi-fi", "fr-fr", "de-de",
    "gr-el", "hk-tzh", "hu-hu", "in-en", "id-id", "id-en", "ie-en", "il-he", "it-it", "jp-jp", "kr-kr",
    "lv-lv", "lt-lt", "xl-es", "my-ms", "my-en", "mx-es", "nl-nl", "nz-en", "no-no", "pe-es", "ph-en",
    "ph-tl", "pl-pl", "pt-pt", "ro-ro", "ru-ru", "sg-en", "sk-sk", "sl-sl", "za-en", "es-es", "se-sv",
    "ch-de", "ch-fr", "ch-it", "tw-tzh", "th-th", "tr-tr", "ua-uk", "uk-en", "us-en", "ue-es", "ve-es",
    "vn-vi",
]


def configure_ollama():
    load_dotenv(find_dotenv())
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            "api_key": os.getenv("OLLAMA_API_KEY", "ollama"),
            "model": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"),
            "model_type": "chat",
            "request_options": {"temperature": 0.2},
        },
    )


def build_agent():
    configure_ollama()
    agent = Agently.create_agent()
    agent.set_agent_prompt(
        "system",
        "Use Search to discover candidate pages. Use Browse only when you need page content. "
        "Quote source URLs in the final answer when action results include them.",
    )
    agent.set_action_loop(max_rounds=4)
    disable_jina_reader = os.getenv("BROWSE_DISABLE_JINA_READER", "0").lower() in {"1", "true", "yes"}
    jina_reader_endpoint = os.getenv("BROWSE_JINA_READER_ENDPOINT", "https://r.jina.ai/")
    browse_fallback_order = (
        ("playwright", "bs4", "curl") if disable_jina_reader else ("jina_reader", "playwright", "bs4", "curl")
    )
    agent.use_actions(
        [
            Search(
                proxy=os.getenv("SEARCH_PROXY") or None,
                timeout=15,
                backend=cast(SearchBackend, os.getenv("SEARCH_BACKEND", "auto")),
                region=cast(SearchRegion, os.getenv("SEARCH_REGION", "us-en")),
            ),
            Browse(
                proxy=os.getenv("BROWSE_PROXY") or os.getenv("SEARCH_PROXY") or None,
                enable_pyautogui=False,
                enable_playwright=True,
                enable_curl=True,
                enable_jina_reader=not disable_jina_reader,
                enable_bs4=True,
                jina_reader_endpoint=jina_reader_endpoint,
                fallback_order=browse_fallback_order,
            ),
        ]
    )
    return agent


def main():
    agent = build_agent()
    turn = agent.input(
        "Find one recent source about agent action runtime design, browse the most relevant page if needed, "
        "then summarize the key point in two bullets."
    )
    records = agent.get_action_result(prompt=turn.prompt)
    print("[ACTION_RECORDS]")
    pprint(records)

    result = turn.get_result()
    print("[MODEL_REPLY]")
    print(result.get_text())

    extra = result.full_result_data.get("extra") or {}
    print("[ACTION_LOGS]")
    pprint(extra.get("action_logs", extra.get("tool_logs", [])) if isinstance(extra, dict) else [])


if __name__ == "__main__":
    main()

# Expected key output with Ollama and search/browse dependencies configured:
# [ACTION_RECORDS] includes Search actions and may include Browse if the model needs page content.
# Search/Browse instruction-heavy records include model_digest and artifact_refs.
# Browse backend traces show Jina Reader, Playwright, BS4, and restricted curl by default.
# They omit Jina Reader when BROWSE_DISABLE_JINA_READER=1.
# [MODEL_REPLY] summarizes the discovered source and quotes source URLs when available.

# How it works:
# Both SearchPack and BrowsePack are registered and the model decides which to call.
# Typically the model calls search first to find relevant URLs, then calls browse on
# the most relevant page to extract deeper content. Browse uses Jina Reader as
# the default external URL-to-Markdown first pass, then falls back to local
# backends when Reader transport fails or returns an obvious block/error shell.
# Set BROWSE_DISABLE_JINA_READER=1 when that service boundary is not acceptable.
# Set BROWSE_JINA_READER_ENDPOINT to choose the primary Reader endpoint; Browse
# also knows the official alternate endpoint https://r.jinaai.cn/. get_action_result()
# drives the full multi-round action loop; get_result() asks the model to summarize
# with the action results injected. The model's "extra.action_logs" captures the
# full trace.
