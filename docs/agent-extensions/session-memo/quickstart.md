---
title: Session Quickstart
description: "Agently Session quickstart for v4.0.8.1+: toggling sessions, isolation, selective recording, restore, and custom memo strategy."
keywords: "Agently,Session,quickstart,activate_session,session.input_keys"
---

# Session Quickstart

> Applies to: 4.0.8.1+

This page walks through the practical flow in v4.0.8.1:

1. turn session on/off
2. isolate sessions by id
3. control recording fields
4. export and restore
5. customize window/memo strategy

## 1) Turn session on/off

```python
from agently import Agently

agent = Agently.create_agent()

agent.activate_session(session_id="demo_on_off")
agent.input("Remember: buy eggs tomorrow.").streaming_print()
agent.input("What should I buy tomorrow?").streaming_print()

agent.deactivate_session()
agent.input("What should I buy tomorrow?").streaming_print()
```

Notes:

- `activate_session()` auto-generates an id if omitted
- same id reuses history, different ids isolate history

## 2) Session isolation by id

```python
agent.activate_session(session_id="trip_a")
agent.input("Remember trip A destination: Tokyo.").streaming_print()

agent.activate_session(session_id="trip_b")
agent.input("Remember trip B destination: Paris.").streaming_print()

agent.input("What is my destination?").streaming_print()  # trip_b

agent.activate_session(session_id="trip_a")
agent.input("What is my destination?").streaming_print()  # trip_a
```

For web apps, use stable keys (`user_id`, `tenant:user_id`) as `session_id`.

## 3) Selective input/output recording

By default, full prompt/result is recorded. To record only key fields:

```python
agent.activate_session(session_id="record_demo")
agent.set_settings("session.input_keys", ["info.task", "input.lang"])
agent.set_settings("session.reply_keys", ["summary", "keywords"])

result = (
    agent
    .info({"task": "Summarize Agently", "style": "technical"})
    .input({"lang": "en", "noise": "ignored"})
    .output({"summary": (str,), "keywords": [(str,)]})
    .get_data()
)

print(result)
print(agent.activated_session.full_context)

agent.set_settings("session.input_keys", None)
agent.set_settings("session.reply_keys", None)
```

## 4) Export and restore

```python
from agently.core import Session

agent.activate_session(session_id="export_demo")
agent.input("Remember recovery code: X-2025-ABCD").streaming_print()

exported = agent.activated_session.get_json_session()

restored = Session(settings=agent.settings)
restored.load_json_session(exported)
restored.id = "export_demo_restored"
agent.sessions[restored.id] = restored
agent.activate_session(session_id=restored.id)

agent.input("What recovery code did I ask you to remember?").streaming_print()
```

## 5) Custom window strategy and memo

```python
def analysis_handler(full_context, context_window, memo, session_settings):
    if len(context_window) > 4:
        return "keep_last_four"
    return None

async def keep_last_four(full_context, context_window, memo, session_settings):
    kept = list(context_window[-4:])
    new_memo = {"kept_count": len(kept)}
    return None, kept, new_memo

agent.activate_session(session_id="memo_demo")
session = agent.activated_session
session.register_analysis_handler(analysis_handler)
session.register_execution_handlers("keep_last_four", keep_last_four)
```

Continue with:

- [Memo Design & Updates](/en/agent-extensions/session-memo/memo)
- [Resize & Strategy Extension](/en/agent-extensions/session-memo/resize)
