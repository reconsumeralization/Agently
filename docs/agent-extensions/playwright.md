---
title: Playwright Tool
description: "Agently Playwright built-in tool docs: rendered-page reading, link extraction, screenshots, and Agent integration."
keywords: "Agently,Playwright,browser automation,tool calling,agent.use_tools"
---

# Playwright Tool

> Applies to: 4.0.8.1+

`Playwright` is Agently's built-in browser tool for pages where plain HTTP fetch is not enough:

- run frontend JS before reading content
- capture title, final URL, and response status
- optionally save screenshots and collect links

## 1. Initialization

```python
from agently.builtins.tools import Playwright

playwright = Playwright(
    headless=True,
    timeout=30000,
    proxy=None,
    user_agent=None,
    response_mode="markdown",  # "markdown" | "text"
    max_content_length=8000,
    include_links=False,
    max_links=120,
    screenshot_path=None,
)
```

Key options:

- `response_mode`: `markdown` converts anchors into markdown links; `text` returns plain text
- `include_links`: include `links` in output
- `screenshot_path`: save full-page screenshot

## 2. Direct usage

```python
import asyncio
from agently.builtins.tools import Playwright

playwright = Playwright(headless=True, response_mode="markdown")

async def main():
    result = await playwright.open("https://agently.tech")
    print(result)

asyncio.run(main())
```

## 3. Use with Agent

```python
from agently import Agently
from agently.builtins.tools import Playwright

agent = Agently.create_agent()
playwright = Playwright(headless=True, response_mode="markdown")

agent.use_tools([playwright.open])
result = agent.input("Browse agently.tech and summarize TriggerFlow").start()
print(result)
```

> When registered through `tool_info_list`, the tool name is `playwright_open`.

## 4. Output shape (success)

Typical fields:

- `ok`
- `requested_url`
- `normalized_url`
- `url` (final URL)
- `status`
- `title`
- `content_format`
- `content`
- `screenshot_path`
- `links` (only when `include_links=True`)

On failure, it returns `ok=False` and `error`.

## 5. Recommendations

- install browser runtime first (`playwright install`)
- tune `timeout` and `proxy` for stability
- for precise DOM extraction, consider dedicated selectors/workflows instead of only `content`
