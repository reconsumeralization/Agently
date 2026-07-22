---
title: Agently 4.1.4.3 Release Notes
description: Direct and nested Pydantic v2 output-model compatibility for ModelRequest and AgentExecution.
keywords: Agently, 4.1.4.3, Pydantic, structured output, ModelRequest, AgentExecution
---

# Agently 4.1.4.3 Release Notes

Agently 4.1.4.3 is a focused compatibility patch for structured output. It
preserves the public APIs and owner boundaries from 4.1.4.2.

## Pydantic output models

Passing a Pydantic v2 `BaseModel` class directly to `.output(...)` could fail
during request initialization, before the provider was called. The prompt
generator treated the class as a generic Python type and attempted to
instantiate it without its required fields.

4.1.4.3 recognizes `BaseModel` classes before generic type handling, recursively
projects nested fields into the prompt schema, and preserves the original model
class as the result model.

```python
class MeetingMinutes(BaseModel):
    topic: str
    decisions: list[Decision]

minutes = (
    agent
    .input(meeting_text)
    .output(MeetingMinutes, format="json")
    .get_result()
    .get_data_object()
)

assert isinstance(minutes, MeetingMinutes)
```

The same contract is supported by direct `ModelRequest` usage and
`AgentExecution.output(...)`. Existing dictionary, tuple, JSON Schema, and
explicit nested output descriptions remain supported.

## Validation

- Regression coverage includes direct requests, nested Pydantic models, and
  AgentExecution direct strategy.
- The release candidate was built as a wheel and installed into an isolated
  Python 3.10 environment before publication.
- The complete repository suite and static type gate passed for the underlying
  fix before release preparation.

Related issue: [#329](https://github.com/AgentEra/Agently/issues/329).

## Compatibility

- Package version: `4.1.4.3`.
- Release manifest: `compatibility/releases/4.1.4.3.json`.
- Python: `>=3.10`.
- Recommended DevTools version remains `agently-devtools >=0.1.10,<0.2.0`.
