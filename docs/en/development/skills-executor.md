---
title: Skills Executor
description: Standard SKILL.md packages installed and executed through Agently.
keywords: Agently, Skills Executor, SKILL.md, skills, run_skills_task, use_skills
---

# Skills Executor

> Languages: **English** · [中文](../../cn/development/skills-executor.md)

Agently Skills follow the standard Skills layout: `SKILL.md` is the capability
definition, with optional `scripts/`, `references/`, and `assets/` resources.
Agently does not define a separate Skill authoring manifest.

```markdown
---
name: release-review
description: Use when checking release readiness and rollback risk.
---

# Release Review

Follow this checklist before recommending a release or rollback...
```

## Install

`install_skills(...)` copies the standard Skill directory into the local
registry. The installed Skill root still contains `SKILL.md` directly. Agently
adds its own management files under `.agently/` inside the installed copy.

```text
.agently/skills/release-review/
|-- SKILL.md
|-- scripts/
|-- references/
|-- assets/
`-- .agently/
    |-- install.json
    |-- decision_card.json
    |-- resource_index.json
    `-- checksums.json
```

The `.agently/` files speed up routing and inspection. They are not Skill
capability definitions. If a derived file is missing or stale, Agently rebuilds
or falls back to reading `SKILL.md`.

`skill_id` is derived from the `SKILL.md` frontmatter `name`: lowercase,
whitespace becomes `-`, and only `a-z0-9._-` remains. Use the returned
`contract["skill_id"]` when wiring later calls.

```python
contract = Agently.skills_executor.install_skills("./release-review")
agent.use_skills([contract["skill_id"]], mode="model_decision")
```

Root-level non-standard manifests such as `skill.yaml`, `skill.json`, or
`agently.skill.yaml` are rejected. Files with those names inside `scripts/`,
`references/`, or `assets/` are treated as ordinary resources.

## Select

Use `use_skills(...)` to expose installed Skills as optional route candidates.
The model sees concise decision cards first; full guidance is used only when the
Skills route executes.

```python
agent = Agently.create_agent("ops-assistant")
agent.use_skills(["release-review"], mode="model_decision")
```

Use `resolve_skills_plan(...)` when you need to inspect which Skills would be
used. Required Skills keep the caller-provided order. Multiple optional
candidates are ordered by the model.

```python
plan = await agent.async_resolve_skills_plan(
    "Should this release be blocked?",
    skills=["release-review", "incident-triage"],
    mode="model_decision",
)
```

## Execute

Use `run_skills_task(...)` when the task must be answered through selected
Skills. By default, execution is `single_shot`: Agently injects the selected
`SKILL.md` guidance, decision cards, resource summaries, and the task into one
model request. Skills that declare `execution: staged` or `allowed-tools` can
run through the TriggerFlow-backed `staged` and `react` strategies.
When actions are available, `react` delegates tool/action planning and
execution to the Agent ActionRuntime, so kwargs schemas, MCP tools, policy,
approvals, concurrency, and execution-environment handling stay on the Action
layer instead of being reimplemented by Skills.

```python
execution = await agent.async_run_skills_task(
    "Review this release and give a go/no-go recommendation.",
    skills=["release-review"],
    mode="required",
)

print(execution.status)
print(execution.output)
print(execution.skill_logs)
```

`semantic_outputs=` uses the same schema grammar as `.output(...)`; it is the
structured-output schema for the Skill run:

```python
execution = await agent.async_run_skills_task(
    "Write a release decision.",
    skills=["release-review"],
    mode="required",
    semantic_outputs={"decision": (str, "go or no-go", True)},
)
```

Agent prompt methods are also supported for explicit Skills execution. The
Skill run consumes the current prompt snapshot, uses rendered prompt text as the
task, and maps the `output` / `output_format` slots to `semantic_outputs` /
`output_format`:

```python
execution = await (
    agent
    .info({"release": "4.1.2.x"})
    .input("Write a release decision.")
    .output({"decision": (str, "go or no-go", True)}, format="json")
    .async_run_skills_task(skills=["release-review"], mode="required")
)
```

`set_agent_prompt(...)` values are inherited and kept for later turns.
`set_request_prompt(...)` / quick prompt values are frozen into the Skill run
and then cleared from the pending request. Explicit `semantic_outputs=` and
`output_format=` arguments override prompt-derived defaults.

`output_format=` selects how that model response is controlled. Leave it as
`"auto"` for ordinary Skill answers. Auto is structural: it chooses
`"flat_markdown"` for flat string-only schemas, `"hybrid"` for top-level dicts
that combine string fields with complex list/object fields, and `"json"` for
boolean/numeric control fields, all-complex schemas, and non-dict outputs. Use
explicit `"json"` for compact machine-readable results, judges, booleans,
numbers, or downstream JSON-only contracts.

```python
execution = await agent.async_run_skills_task(
    "Draft a release announcement as HTML.",
    skills=["release-review"],
    mode="required",
    semantic_outputs={"html": (str, "render-ready HTML", True)},
    output_format="flat_markdown",
)
```

For fixed required fields, prefer the third tuple element in the schema:

```python
semantic_outputs = {
    "rules": [
        {
            "rule_id": (str, "Stable rule id", True),
            "passed": (bool, "Whether this rule passed", True),
            "evidence": (str, "Concise evidence; empty string is allowed", False),
        }
    ],
    "passes": (bool, "Overall pass/fail", True),
}
```

Use runtime `ensure_keys=` only for paths that are conditional or decided at
runtime. `max_retries=3` means Agently can make up to three additional model
attempts when parsing, required keys, strict output validation, or custom
validators fail. Retries often recover ordinary omissions, markdown header
mistakes, and auto-format degradation to JSON. They can still fail after all
attempts when a model repeatedly echoes placeholder scaffolding, returns prose
for boolean or numeric fields, produces malformed nested arrays, truncates a
large prompt, or must fill many wildcard paths such as
`rule_results[*].evidence`. For model judges with many rules, prefer
`output_format="json"`, keep the schema shallow when possible, and split very
large rule sets into smaller judge calls.

Direct Skills execution streams runtime items through `stream_handler`:

- `skills.prompt_only.start`
- `skills.model_stream` with `path`, `value`, `delta`, and `is_complete`
- `skills.prompt_only.done`
- `skills.staged.*`, `skills.react.*`, and `block.*` events when a multi-step
  strategy is selected

Use `effort=` with `agent.set_settings("effort_presets", {...})` to map a
caller-facing quality/cost profile to strategy, model key, step budget, and
artifact inline limit:

```python
agent.set_settings("effort_presets", {
    "fast": {"strategy": "single_shot", "reason_key": "reason_fast", "step_budget": 1},
    "normal": {"strategy": "staged", "reason_key": "reason", "step_budget": 5},
})

execution = await agent.async_run_skills_task(
    "Draft a release decision.",
    skills=["release-review"],
    mode="required",
    effort="normal",
)
```

`reason_key` is a symbolic model-pool key. If it is not mapped in
`model_pool`, Agently leaves the request on the agent's inherited model instead
of sending the symbolic key as a provider model name.

When Skills are selected through Agent auto-orchestration, model field stream
items are bridged to stable paths like `skills.model.fields.<field_path>`.

Bundled scripts and resources are never executed just because a Skill is
installed. They can only be used through explicit Action or Execution
Environment paths chosen by the host application.

## Settings

Skill applicability comes from `SKILL.md`; Agently's `.agently/` files are
descriptive install metadata only. Multi-step Skills execution composes
Agently's existing TriggerFlow, Action, and ExecutionEnvironment boundaries;
human approval or durable wait/resume flows should be modeled through
TriggerFlow `pause_for(...)` / `continue_with(...)` or Action /
ExecutionEnvironment approval policies, not by mutating a closed
`SkillExecution` snapshot.

Framework-level `skills.*` settings may still tune host behavior, such as
whether optional Skill candidates disclose full guidance in ordinary prompts.
Plugin defaults load first when present; framework settings are the final
application-level defaults. Neither setting layer can replace `SKILL.md` as the
Skill capability definition.

Use the public Skills Executor configuration helper for local registry options:

```python
Agently.skills_executor.configure(
    registry_root="./.agently/skills-dev",
    allowed_trust_levels=["local"],
)
```

## API Summary

- `Agently.skills_executor.install_skills(...)`
- `Agently.skills_executor.install_skills_pack(...)`
- `Agently.skills_executor.configure(...)`
- `Agently.skills_executor.inspect_skills(...)`
- `agent.use_skills(...)`
- `agent.use_skills_packs(...)`
- `agent.resolve_skills_plan(...)`
- `agent.run_skills_task(...)`

`SkillContract` describes the installed standard Skill, Agently install
metadata, decision card, resource index, and checksums. It does not contain
framework-authored stage declarations.
