---
title: Troubleshooting
description: "Session troubleshooting for v4.0.8.1+: migration errors, missing memory, empty selectors, memo updates, and import failures."
keywords: "Agently,Session,troubleshooting,activate_session,memo"
---

# Troubleshooting

> Applies to: 4.0.8.1+

## 1) Error: Session method missing

Cause: code still uses legacy helper style.

Fix:

- enable session: `activate_session(session_id=...)`
- disable session: `deactivate_session()`
- bounded context: `session.max_length` + `resize`

## 2) Session does not "remember"

Check in order:

1. `activate_session(...)` is called
2. session was not deactivated before request
3. correct `session_id` is active
4. `context_window` was not over-pruned by strategy

Quick inspection:

```python
print(agent.activated_session.id)
print(len(agent.activated_session.full_context))
print(len(agent.activated_session.context_window))
```

## 3) `session.input_keys` / `reply_keys` produce empty records

Common causes:

- wrong path
- mixed path style (`a.b` vs `a/b`)
- expected field not present in result payload

Recommendation: temporarily set selectors to `None` to inspect full structure, then narrow.

## 4) Memo never updates

In v4.0.8.1+, memo is not auto-generated.

Verify:

- analyzer returns a strategy name
- executor returns `new_memo` as tuple third value

## 5) Session import fails

Possible causes:

- payload is not a dict
- `session_key_path` does not point to session object
- encoding mismatch for file reads

Recommendation:

- test with direct JSON string first
- then move to file path + key path

## 6) Context cost keeps growing

Set length limit:

```python
agent.set_settings("session.max_length", 12000)
```

If needed, add custom strategy to keep only latest N turns.
