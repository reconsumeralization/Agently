---
title: Quickstart
description: "Agently quickstart for ordinary developers: install, model setup, structured output, and the next handbook steps."
keywords: "Agently,quickstart,structured output,model setup,Async First"
---

# Quickstart

This is the first lesson in the Agently handbook. The goal is not to explain every capability at once. The goal is to get one **minimal but correct** path running in a few minutes.

## When to read this

- You are new to Agently
- You want to confirm that installation, model setup, and one minimal request all work
- You want a clear next step after the first successful run

## What you will learn

- How to install Agently
- How to complete minimal model setup
- How to get structured output with `input()` + `output()` + `start()`
- Where to go next in the handbook

> [!TIP]
> The minimal example on this page intentionally uses a sync call to reduce first-run friction. Once you move into services, streaming UI, SSE, or TriggerFlow orchestration, switch to the async path in [Async First](/en/async-support).

## 1. Install

```bash
pip install -U agently
```

You can also use:

```bash
uv pip install -U agently
```

## 2. Model setup

Agently v4 commonly starts with `OpenAICompatible`. Set `base_url + api_key + model` first:

```python
from agently import Agently

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "https://api.openai.com/v1",
        "api_key": "YOUR_OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
)
```

For more providers, continue with [Model Settings](/en/model-settings).

## 3. Run one minimal structured request

```python
from agently import Agently

agent = Agently.create_agent()

result = (
    agent
    .input("Write a one-line positioning and two product highlights for Agently")
    .output(
        {
            "Positioning": (str, "One-line positioning"),
            "Highlights": [
                {
                    "Title": (str, "Highlight title"),
                    "Detail": (str, "One-line detail"),
                }
            ],
        }
    )
    .start()
)

print(result)
```

You should get a structured object instead of free-form text that is hard to consume reliably.

## 4. What to learn next

After the example above works, continue in this order:

1. [Model Settings](/en/model-settings)
2. [Output Control Overview](/en/output-control/overview)
3. [Model Response Overview](/en/model-response/overview)
4. [Prompt Management Overview](/en/prompt-management/overview)

If you already know this will run inside a web service, streaming UI, or workflow engine, treat this as a parallel main path:

1. [Async First](/en/async-support)
2. [Instant Structured Streaming](/en/output-control/instant-streaming)
3. [TriggerFlow Overview](/en/triggerflow/overview)

## Common mistakes

- Jumping into TriggerFlow before the request side works
- Writing a custom JSON parser before using `output()`
- Putting prompts, settings, and business logic into one script from the start

## Next

- More providers: [Model Settings](/en/model-settings)
- More stable fields: [Output Control Overview](/en/output-control/overview)
- Streaming, text, data, and metadata: [Model Response Overview](/en/model-response/overview)
- Recommended production path: [Async First](/en/async-support)
- Maintainable project layout: [Project Framework](/en/project-framework)

## Related Skills

- `agently-model-setup`
- `agently-output-control`
- `agently-model-response`
