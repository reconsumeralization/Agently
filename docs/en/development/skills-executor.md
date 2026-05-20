---
title: Skills Executor
description: Planner-selected declarative behavior loops exposed through Agent APIs.
keywords: Agently, Skills Executor, skills, behavior loop, run_skill_task, use_skills
---

# Skills Executor

> Languages: **English** · [中文](../../cn/development/skills-executor.md)

Skills Executor lets an Agently app install declarative skill packages and use
them as planner-selected behavior loops.

It is separate from the `Agently-Skills` companion repository:

- **Skills Executor** is framework runtime capability inside Agently.
- **Agently-Skills** is companion guidance for external coding agents such as
  Codex, Claude Code, and Cursor.

## Current Status

This feature is being implemented on `feature/skills-executor`.

The current implementation provides:

- `Agently.skills.install(...)`, `list()`, `inspect()`, and `remove()`
- `agent.use_skills(...)` for optional model-decision skill card disclosure
- bounded primary `SKILL.md` guidance disclosure for selected candidate skills
- `agent.resolve_skill_plan(...)` for producing a `SkillExecutionPlan`
- `agent.run_skill_task(...)` for explicit skill task execution
- declarative `action`, `model`, `validate`, and `emit` stage handling

The first implementation keeps model-owned planning behind the plan/decision
boundary. It uses deterministic filtering and allows an app decision handler to
adjust the plan. Full model planner integration should land behind the same
`SkillExecutionPlan` contract.

## User Mental Model

A Skill is not a `skill.run()` function and it is not an `ActionExecutor`.

```text
Agent API
  -> skill cards and policy filtering
  -> SkillExecutionPlan
  -> SkillExecution
  -> Actions for atomic work
```

Action calls remain atomic capabilities. Skills compose those capabilities into
a behavior loop.

## Optional Skills

Use `use_skills(...)` when skills are optional candidates for normal agent
requests.

```python
agent = Agently.create_agent("ops-assistant")

agent.use_skills(
    ["release-checklist", "incident-triage"],
    mode="model_decision",
)

response = await (
    agent
    .input("Should this production issue trigger rollback?")
    .get_response()
    .async_get_text()
)
```

This discloses skill cards. It does not force the agent to execute a skill.
For SKILL.md packages, the primary guidance body is also disclosed with a
per-skill character limit so the model can use checklist details without
loading arbitrary scripts or resource folders.

## Required Skill Task

Use `run_skill_task(...)` when a task must be handled through a skill loop.

```python
execution = await agent.async_run_skill_task(
    "prepare release notes",
    skills=["release-checklist"],
    mode="required",
)

print(execution.status)
print(execution.output)
print(execution.action_logs)
```

In `required` mode, requested skills must be selected. Missing dependencies,
denied permissions, or unavailable actions fail closed.

## Minimal Skill Package

```yaml
skill_id: release-checklist
display_name: Release Checklist
purpose: Check release readiness and record a release note.
trust_level: local
activation:
  keywords: [release, rollback]
requires:
  actions: [record_release_note]
stages:
  - id: record_note
    kind: action
    action: record_release_note
    input:
      text: "${task}"
  - id: validate_note
    kind: validate
    validation:
      required_state: [record_note]
```

## Boundaries

- Scripts and helpers must run through controlled Actions, not arbitrary Skill
  Python handlers.
- Guidance disclosure is prompt context, not code execution.
- MCP, browser, sandbox, process, and credential resources belong to Execution
  Environment.
- Long-running workflow behavior should be represented by TriggerFlow-backed
  skill execution, not hidden inside a Skill package.

## Tested External Skill Packages

`examples/skills_executor/deepseek_external_skill_cards.py` installs and tests:

- `../Agently-Skills/skills/agently-runtime`
- `anthropics/skills/skills/xlsx`
- `anthropics/skills/skills/webapp-testing`

The example verifies that DeepSeek receives the selected skill card and bounded
primary guidance in `model_decision` mode, while package scripts remain inert
assets unless the app binds them through Actions.

`examples/skills_executor/realcase_dynamic_todo_triggerflow.py` is the
diagnostic realcase variant. It installs `Agently-Skills`, exposes
`agently-playbook`, `agently-request`, and `agently-triggerflow` to DeepSeek
through `agent.use_skills(...)`, then asks DeepSeek to generate both the Todo
DAG and a complete Python TriggerFlow executor module. The prompt intentionally
does not spell out TriggerFlow API details; the host script evaluates whether
the model-generated module used real Agently APIs and whether it ran.

`examples/skills_executor/combo_skillpack_diagnostics.py` is the combo Skill
Pack benchmark. It validates realcase orchestration pressure points across:

- education course pack generation
- stock research pack generation
- travel planning with approval before external writes
- research report to spreadsheet, document, deck, and PDF
- web app acceptance testing evidence packs

The combo benchmark uses real local `SKILL.md` packages when available and
skips missing public sources rather than substituting mock skills. It can fetch
the referenced public repositories with `--fetch-missing`. The model receives
only optional SkillCards plus bounded primary guidance; the host evaluator
checks skill selection, stage switching, intermediate artifacts, side-effect
boundaries, approval gates, fallbacks, and output coverage.

## See Also

- [Coding Agents](coding-agents.md)
- [Action Runtime](../actions/action-runtime.md)
- [Execution Environment](../actions/execution-environment.md)
- [TriggerFlow Overview](../triggerflow/overview.md)
