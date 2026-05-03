---
title: Memo Design & Updates
description: "How to maintain memo in Session v4.0.8.1+ with custom strategy handlers."
keywords: "Agently,Session,memo,register_execution_handlers,create_temp_request"
---

# Memo Design & Updates

> Applies to: 4.0.8.1+

Memo is no longer a fixed built-in algorithm. You now own the memo schema and update strategy.

## 1) Design principles

Keep memo for stable, reusable context:

- long-term user preferences
- durable constraints
- confirmed facts

Avoid dumping transient turn-by-turn details into memo.

## 2) Typical update triggers

Common choices:

- when `context_window` exceeds threshold
- at workflow stage boundaries
- every N turns

## 3) LLM-driven memo update pattern

```python
from agently import Agently

agent = Agently.create_agent()
agent.activate_session(session_id="memo_demo")
session = agent.activated_session


def analysis_handler(full_context, context_window, memo, session_settings):
    if len(context_window) > 6:
        return "compress_with_memo"
    return None


async def compress_with_memo(full_context, context_window, memo, session_settings):
    kept = list(context_window[-4:])

    memo_request = agent.create_temp_request()
    (
        memo_request
        .input({
            "history_to_compress": [m.model_dump() for m in context_window[:-4]],
            "old_memo": memo,
        })
        .instruct("Merge history_to_compress with old_memo into a stable structured memo.")
        .output({
            "user_preferences": {"<key>": (str, "value"), "...": "..."},
            "stable_facts": [{"fact": (str,), "confidence": (float,)}],
        })
    )

    new_memo = await memo_request.async_start(ensure_keys=["user_preferences", "stable_facts"])
    return None, kept, new_memo


session.register_analysis_handler(analysis_handler)
session.register_execution_handlers("compress_with_memo", compress_with_memo)
```

## 4) How memo reaches the model

When `session.memo` is not `None`, SessionExtension injects it under:

- `CHAT SESSION MEMO`

So you do not need manual `agent.info({"memo": ...})` injection.

## 5) Production recommendations

- define a stable memo schema
- add fallback when memo generation fails
- keep memo compact
- test key fields explicitly
