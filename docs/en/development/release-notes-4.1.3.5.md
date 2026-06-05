---
title: Agently 4.1.3.5 Release Notes
description: Agently 4.1.3.5 release notes for settings-owned structured output defaults, meaningful required values, AgentTurn prompt isolation, and set_turn_prompt semantics.
keywords: Agently, release notes, 4.1.3.5, structured output, AgentTurn, set_turn_prompt, DevTools
---

# Agently 4.1.3.5 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.5.md)

Agently 4.1.3.5 is a release-line stability slice for the request foundation.
It focuses on two root contracts: structured output parsing must be stable
across local models, and one Agent instance must be safe to reuse while each
turn keeps its request prompt isolated.

## What Changed

### Structured output defaults are settings-owned

Omitted `.output(..., format=...)` now reads
`prompt.default_output_format` from the active settings chain. The global
default remains `json`. Explicit `format="auto"` and
`prompt.default_output_format="auto"` are still available when the caller has
validated the target model and schema family.

This release keeps `auto` available but no longer treats it as the default
strategy. Recent local `qwen2.5:7b` checks showed that hybrid-style responses
can omit required section headers or echo scaffold comments into text fields, so
the framework default stays conservative.

### Required fields must contain meaningful values

Tuple `ensure=True` and runtime `ensure_keys` now require the target path to
resolve to a meaningful value. Missing keys, `None`, blank strings, empty
wildcard matches, and wildcard matches containing blank required values fail
validation and consume the normal retry budget. Typed falsey values such as
`False` and `0` remain valid.

### AgentTurn owns request-scoped prompt drafts

Non-`always=True` Agent quick prompt chains create an isolated `AgentTurn`
request draft. Expression-local chaining remains concise:

```python
result = await agent.input("Review this ticket.").output({
    "answer": (str, "final answer", True),
}).async_start()
```

When setup is split across statements, conditionals, or helper functions, hold
the turn explicitly:

```python
turn = agent.create_turn()
turn.input("Review this ticket.")
turn.output({"answer": (str, "final answer", True)})
result = await turn.async_start()
```

### `set_turn_prompt(...)` names the one-turn write surface

`set_turn_prompt(...)` is now the recommended name for writing a prompt slot
for one turn. `set_request_prompt(...)` remains a compatibility alias with the
same behavior and is not deprecated.

Prompt config files now accept `.turn` as the recommended turn-scoped section;
`.request` remains accepted for compatibility.

```yaml
.turn:
  input: Review this ticket.
  output:
    $format: json
    answer:
      $type: str
      $ensure: true
```

### Bash sandbox action descriptions include policy limits

`register_bash_sandbox_action(...)` and `agent.enable_shell(...)` now append
the model-visible command prefix allowlist, allowed working-directory roots, and
timeout to the action description. This keeps shell capability boundaries visible
to the model when the action is exposed.

## Compatibility

- Package version: `4.1.3.5`.
- Release manifest: `compatibility/releases/4.1.3.5.json`.
- Recommended `agently-devtools`: `>=0.1.7,<0.2.0`.
- `set_request_prompt(...)` and `.request` prompt config remain compatible.

## Validation Summary

- Editable install smoke against the course script with local `qwen2.5:7b`
  passed repeated structured-output checks without missing key sections or
  scaffold comments.
- Targeted regression tests cover settings-owned output defaults, meaningful
  required values, AgentTurn request-scoped prompt drafts, and
  `set_turn_prompt(...)` compatibility.
- Agently-Skills guidance was updated and validated with the companion
  validation suite.

## Deferred Scope

This release does not merge `response` and `result`. `ModelResponse` already
proxies the common result readers, while `response.result` remains the stable
cache/materialization facade for text, data, metadata, validation, and
streaming.
