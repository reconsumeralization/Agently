---
title: Session Toggle & Unified Usage
description: "Session usage guide for v4.0.8.1+: activation, restore, and bounded context patterns."
keywords: "Agently,Session,activate_session,deactivate_session,session management"
---

# Session Toggle & Unified Usage

> Applies to: 4.0.8.1+

This page standardizes team usage for v4.0.8.1+ Session APIs.

## 1) Unified model

- start session: `activate_session(session_id=...)`
- stop session: `deactivate_session()`
- bound window: `session.max_length` + `resize()`
- maintain memory: custom analysis/execution handlers updating `memo`
- restore sessions: `load_json_session/load_yaml_session` + activate from session pool

## 2) Recommended toggle pattern

```python
from agently import Agently

agent = Agently.create_agent()

agent.activate_session(session_id="user_1001")
agent.input("Remember: reply in concise Chinese.").streaming_print()

agent.deactivate_session()
```

## 3) Restore from snapshot

```python
from agently import Agently
from agently.core import Session

agent = Agently.create_agent()

restored = Session(settings=agent.settings)
restored.load_json_session("session.snapshot.json")

agent.sessions[restored.id] = restored
agent.activate_session(session_id=restored.id)
```

## 4) Record-only behavior in new API

If you want "record without truncation", keep `session.max_length=None` (default).

```python
agent.activate_session(session_id="record_only")
agent.set_settings("session.max_length", None)
```

## 5) Checklist

- ensure app code consistently uses `activate_session/deactivate_session`
- use `full_context/context_window` field names
- keep memo logic inside custom strategy handlers
