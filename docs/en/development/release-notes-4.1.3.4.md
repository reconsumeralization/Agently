---
title: Agently 4.1.3.4 Release Notes
description: Agently 4.1.3.4 release notes for structured output parsing hardening, request retry, stream error propagation, runtime capability policy, and the AgentTaskLoop first public slice.
keywords: Agently, release notes, 4.1.3.4, structured output, AgentTaskLoop, PolicyApproval, Skills Executor
---

# Agently 4.1.3.4 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.4.md)

Agently 4.1.3.4 is a release-line hardening slice. The primary release reason
is structured output reliability across local and cloud OpenAI-compatible
models. It also includes runtime capability policy hardening and the first
bounded AgentTaskLoop slice for the 4.1.4 AgentTask target.

## Structured Output Hardening

`.output(..., format="auto")` now selects formats by schema structure only. It
does not inspect field names, business meaning, tokenization, keywords, or model
outputs.

- flat string-only dict schemas resolve to `xml_field`;
- mixed string plus typed non-string schemas resolve to `hybrid`;
- all-complex, all-control, non-dict, and dense machine contracts stay `json`;
- `flat_markdown` remains explicit-only for compatibility;
- `yaml_literal` is explicit opt-in and remains outside auto.

New and revised parser formats:

- `xml_field`: an XML-like field envelope with a custom boundary parser, not a
  strict XML parser;
- `hybrid`: Markdown sections for text fields and fenced JSON values for typed
  fields;
- `yaml_literal`: YAML inside explicit Agently boundaries with literal scalars
  for long text;
- `flat_markdown`: kept for explicit legacy usage, no longer recommended as an
  auto/default path.

Reasoning normalization is handled before parser-specific logic. Provider-native
reasoning fields and a leading outer `<think>...</think>` block before the
payload flow into existing reasoning events; parser payload, code blocks, and
ordinary text retain internal `<think>` content.

## Request And Streaming Reliability

`OpenAICompatible` now retries transient transport failures before output starts.
The default is one replay (`request_retry.max_attempts = 2`). The replay keeps
the same model, prompt, and output format. Once output has started, Agently does
not replay automatically because that could duplicate partial content.

Response materialization now propagates explicit stream/provider construction
errors through `get_text()`, `get_data()`, and `get_meta()` instead of waiting
for the materialization timeout.

## Runtime Capability Policy

Skills capability execution now uses the framework-wide `PolicyApproval`
surface. Skill capability needs are recorded in `SkillExecutionPlan`, host
policy can configure auto-load surfaces, and high-risk capabilities stay behind
approval or fail-closed behavior.

Built-in Search reports `partial_success` when fallback providers recover after
earlier backend failures. `partial_success` remains continuable evidence rather
than an Action failure.

## AgentTaskLoop First Public Slice

`agent.create_task(...)` is available as a bounded single-Agent task loop:
plan one step, execute through `AgentExecution`, write Workspace evidence,
verify, replan if needed, and finish as completed, blocked, or partial after
limits.

This is intentionally a first public slice, not the complete 4.1.4 AgentTask
target. It does not provide multi-task coordination, background autonomy,
distributed leases, or long-term memory management. Use it for bounded
single-agent workflows where the host controls workspace, limits, and enabled
capabilities.

## Compatibility Notes

- Package version: `4.1.3.4`.
- Release manifest: `compatibility/releases/4.1.3.4.json`.
- Agently recommends `agently-devtools >=0.1.7,<0.2.0`.
- Agently-Skills uses authoring protocol `agently-skills.authoring.v2` and
  standard `SKILL.md` packages.
- The next development-line manifest is `compatibility/in-development.json`
  targeting `4.1.3.5`.
