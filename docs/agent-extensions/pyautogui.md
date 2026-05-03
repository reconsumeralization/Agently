---
title: PyAutoGUI Tool
description: "Agently PyAutoGUI built-in tool docs: keyboard-driven browser automation, active-tab reading, and open+read flow."
keywords: "Agently,PyAutoGUI,desktop automation,browser automation,tool calling"
---

# PyAutoGUI Tool

> Applies to: 4.0.8.1+

`PyAutoGUI` drives a real desktop browser session and is useful when user-like interaction is required.

Built-in capabilities:

- `open_url(url)`
- `read_active_tab()`
- `open_and_read_url(url)`

## 1. Initialization

```python
from agently.builtins.tools import PyAutoGUI

pyauto = PyAutoGUI(
    pause=0.05,
    fail_safe=True,
    new_tab=True,
    wait_seconds=1.5,
    dry_run=True,
    type_interval=0.01,
    open_mode="hotkey",         # "hotkey" | "system"
    activate_browser=False,
    browser_app=None,
    activate_wait_seconds=0.4,
    read_wait_seconds=0.4,
    max_content_length=24000,
    response_mode="markdown",   # "markdown" | "text"
)
```

Most important options:

- `dry_run`: default `True`; returns planned actions without executing
- `open_mode`:
  - `hotkey`: control browser via keyboard shortcuts
  - `system`: open URL via system browser handler
- `response_mode`: output format when reading active tab

## 2. Direct usage

```python
import asyncio
from agently.builtins.tools import PyAutoGUI

pyauto = PyAutoGUI(dry_run=False, open_mode="hotkey", activate_browser=True)

async def main():
    opened = await pyauto.open_url("https://agently.tech")
    print("OPEN:", opened)

    page = await pyauto.read_active_tab()
    print("READ:", page)

asyncio.run(main())
```

## 3. Use with Agent

```python
from agently import Agently
from agently.builtins.tools import PyAutoGUI

agent = Agently.create_agent()
pyauto = PyAutoGUI(dry_run=False, open_mode="hotkey", activate_browser=True)

agent.use_tools([
    pyauto.open_url,
    pyauto.read_active_tab,
    pyauto.open_and_read_url,
])

result = agent.input("Open agently.tech and read key points").start()
print(result)
```

> Tool names via `tool_info_list` are:
> `pyautogui_open_url`, `pyautogui_read_active_tab`, `pyautogui_open_and_read_url`.

## 4. Platform and permission limits

- `hotkey` mode requires a real GUI session
- Linux without `DISPLAY` will fail
- `read_active_tab()` currently supports macOS (Darwin) only
- macOS may require permissions:
  - Accessibility / Input Monitoring
  - Automation (terminal controlling browser)

## 5. Recommendations

- validate with `dry_run=True` before enabling real execution
- add timeout and result validation (URL/title/key fields)
- use `PyAutoGUI` as a fallback; prefer `Playwright` for routine web browsing tasks
