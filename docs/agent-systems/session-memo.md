---
title: Session & Memo Playbook
description: "Agently playbook for session memory in v4.0.8.1+: session activation, window control, custom memo strategies, and recovery."
keywords: "Agently,agent systems,Session,memo,playbook"
---

# Session & Memo Playbook

> Applies to: 4.0.8.1+

## Scenario

Production multi-turn systems usually need all three:

- durable user preference memory
- bounded context cost
- recoverable sessions (restart / cross-process)

## Capability (key traits)

- `activate_session(session_id=...)` for session activation and isolation
- `session.max_length` for default window trimming
- `register_analysis_handler/register_execution_handlers` for custom memo strategy
- `get_json_session/load_json_session` for persistence and restore

## Operations

1. activate session by user id
2. set context window limit
3. register compression + memo update strategy
4. inspect `session.memo` after turns
5. export snapshot when needed

## Full code

```python
import json
from agently import Agently

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "model_type": "chat",
    },
).set_settings("request_options", {"temperature": 0.2}).set_settings("debug", False)

agent = Agently.create_agent()
agent.system("You are a product advisor. Reply concisely and keep a structured user profile memo.")

agent.activate_session(session_id="demo_user_xiaohang")
session = agent.activated_session
assert session is not None

agent.set_settings("session.max_length", 12000)


def analysis_handler(full_context, context_window, memo, session_settings):
    if len(context_window) > 6:
        return "compress_with_profile"
    return None


async def compress_with_profile(full_context, context_window, memo, session_settings):
    kept = list(context_window[-4:])
    old_messages = context_window[:-4]

    req = agent.create_temp_request()
    (
        req
        .input(
            {
                "old_messages": [m.model_dump() for m in old_messages],
                "old_memo": memo,
            }
        )
        .instruct("Update user profile memo from old_messages and old_memo. Return structured JSON only.")
        .output(
            {
                "user_profile": {
                    "name": (str,),
                    "project": (str,),
                    "target_users": [(str,)],
                    "preferences": {
                        "reply_style": (str,),
                        "priority": [(str,)],
                    },
                }
            }
        )
    )

    new_memo = await req.async_start(ensure_keys=["user_profile"])
    return None, kept, new_memo


session.register_analysis_handler(analysis_handler)
session.register_execution_handlers("compress_with_profile", compress_with_profile)

reply_1 = agent.input("My name is Xiaohang. I'm building an AI writing tool for students. Give me 2 suggestions.").get_text()
print("TURN1_REPLY:", reply_1)
print("TURN1_MEMO:", json.dumps(session.memo, ensure_ascii=False, indent=2))

reply_2 = agent.input("Budget is limited. What should we prioritize first?").get_text()
print("TURN2_REPLY:", reply_2)
print("TURN2_MEMO:", json.dumps(session.memo, ensure_ascii=False, indent=2))

snapshot = session.get_json_session()
print("SNAPSHOT_LEN:", len(snapshot))
```

## Validation

- switching to another `session_id` keeps history isolated
- `context_window` converges after threshold
- memo schema remains stable and reusable
- exported snapshot can be restored for continued dialogue
