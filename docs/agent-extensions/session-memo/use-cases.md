---
title: Common Use Cases
description: "Practical Session use cases in v4.0.8.1+: multi-user chat, stateless APIs, compliance recording, and long-context memo."
keywords: "Agently,Session,use cases,multi user,stateless API"
---

# Common Use Cases

> Applies to: 4.0.8.1+

## Use case 1: Multi-user chat

Goal: isolate history per user with automatic reuse.

```python
def handle_chat(user_id: str, text: str):
    agent.activate_session(session_id=user_id)
    try:
        return agent.input(text).get_text()
    finally:
        agent.deactivate_session()
```

Tip: in multi-tenant systems, use `tenant:user` composite ids.

## Use case 2: Stateless HTTP APIs + external persistence

Goal: keep backend stateless while preserving context across calls.

```python
from agently.core import Session


def request_once(user_id: str, text: str, load_snapshot: str | None):
    if load_snapshot:
        s = Session(settings=agent.settings)
        s.load_json_session(load_snapshot)
        agent.sessions[s.id] = s
        agent.activate_session(session_id=s.id)
    else:
        agent.activate_session(session_id=user_id)

    reply = agent.input(text).get_text()
    snapshot = agent.activated_session.get_json_session()
    return reply, snapshot
```

## Use case 3: Compliance-friendly minimal recording

Goal: avoid storing sensitive raw text while preserving key business fields.

```python
agent.activate_session(session_id="compliance_demo")
agent.set_settings("session.input_keys", ["input.ticket_id", "input.intent"])
agent.set_settings("session.reply_keys", ["resolution", "risk_level"])

result = (
    agent
    .input({"ticket_id": "T-1001", "intent": "refund", "raw_text": "...PII..."})
    .output({"resolution": (str,), "risk_level": (str,)})
    .get_data()
)
```

## Use case 4: Long conversation with custom memo compression

Goal: keep context bounded and preserve durable memory.

```python
agent.activate_session(session_id="long_chat")
session = agent.activated_session
agent.set_settings("session.max_length", 12000)


def analysis_handler(full_context, context_window, memo, session_settings):
    if len(context_window) > 10:
        return "compress"
    return None


async def compress(full_context, context_window, memo, session_settings):
    kept = list(context_window[-6:])
    new_memo = memo or {}
    new_memo["compressed_rounds"] = new_memo.get("compressed_rounds", 0) + 1
    return None, kept, new_memo


session.register_analysis_handler(analysis_handler)
session.register_execution_handlers("compress", compress)
```

## Use case 5: Cross-runtime handoff (Web -> Worker)

Goal: continue a conversation in async workers.

- export `session_json` in web tier
- enqueue payload
- worker restores with `load_json_session` and continues by `activate_session`
