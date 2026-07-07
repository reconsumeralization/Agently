---
title: Agently 4.1.4.1 Development Notes
description: Agently 4.1.4.1 development notes for AgentExecutionResult business-data and full-data reader compatibility.
keywords: Agently, development notes, 4.1.4.1, AgentExecutionResult, get_data, get_full_data
---

# Agently 4.1.4.1 Development Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.4.1.md)

Agently 4.1.4.1 is the development line after the 4.1.4 release. This page
records accepted in-development behavior as it lands.

## AgentExecution Result Views

`AgentExecutionResult.get_data()` now has the same business-result meaning across
direct, flat, and TaskBoard routes. Direct model-request routes keep returning
the ordinary parsed result. Task-strategy routes that return a terminal envelope
with `final_result` expose that `final_result` through `get_data()`, parsed
against the declared `output(...)` contract when possible.

Use `get_full_data()` / `async_get_full_data()` when caller code needs the full
route/task payload, including `status`, `accepted`, `artifact_status`,
`taskboard`, `completion_notes`, diagnostics, or other execution internals.
`get_text()` / `async_get_text()` still read the full payload so task-strategy
`final_response` remains the preferred user-facing final text.

This fixes the previous mismatch where AgentTask-backed executions could make
`get_data()` return the internal terminal envelope while direct executions
returned the business object.

## Compatibility

- Package target: `4.1.4.1` development line.
- Release manifest: `compatibility/in-development.json`.
- Existing task terminal envelope fields are unchanged; callers that depended
  on them should switch from `get_data()` to `get_full_data()`.
