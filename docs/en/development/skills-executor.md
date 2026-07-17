---
title: Skills and AgentExecution
description: SkillLibrary, TaskContext disclosure, and the thin SkillsExecutor compatibility facade.
keywords: Agently, Skills, SkillLibrary, AgentExecution, TaskContext, SkillsExecutor
---

# Skills and AgentExecution

A real-world Skill is a revisioned knowledge and work-procedure package. Its
`SKILL.md` supplies guidance and its indexed resources may supply references,
examples, assets, or scripts. A Skill is not an execution route, strategy
engine, action grant, or workflow.

## Ownership

| Layer | Owner |
|---|---|
| Install, parse, revision, resolve, list, pack membership | `SkillLibrary` |
| Task-scoped selector intent, exact revision binding, required/model-decision mode | `AgentExecution` |
| Progressive disclosure of guidance/resources | `TaskContext` + `SkillContextSource` + `ContextReader` |
| Model request, AgentTask, TaskDAG, workflow, side effect | Existing execution owner |
| Released management-shaped compatibility calls | `Agently.skills_executor` thin facade |

`SkillLibrary` installs immutable content-addressed revisions. An execution
binds the exact revision, not a mutable directory alias. Skill descriptions may
be offered to a semantic model selector; local code must not route free-form
task text with keyword tables or regular expressions.

## Recommended Agent API

```python
contract = Agently.skills_executor.install_skills(
    "./skills/release-review",
    trust_level="local",
    update=True,
)

execution = (
    agent
    .use_skills([contract["skill_id"]], mode="required")
    .input("Review release candidate 3.2.0.")
    .output({
        "decision": (str, "GO or NO-GO", True),
        "risks": ([str], "Grounded release risks", True),
    })
)
result = await execution.async_get_data()
```

`mode="required"` binds the selected revisions fail-closed. With
`mode="model_decision"`, AgentExecution asks a structured `ModelRequest` to
select from host-issued keys, validates the result, and binds the chosen
revisions. Unknown or duplicate keys fail closed.

`agent.require_skills(...)` is the explicit required-mode convenience method.
`agent.use_skills_packs(...)` expands an installed immutable pack to its pinned
revision refs.

Revision availability is not consumption evidence. AgentTask records a Skill
context consumption only when the disclosed package is attached to a concrete
ModelRequest response. That consumption is context evidence, not an executable
planner capability or Action evidence.

## What `Agently.skills_executor` still does

The facade is retained for released application calls that manage or project
Skills:

- configure the SkillLibrary root and accepted trust labels;
- install, list, inspect, and read local Skill packages;
- install/list/inspect local Skill packs;
- build a compatibility context-pack projection;
- expose the TaskDAG `skill` resolver helper.

It does not own route selection, effort strategies, stages, React loops,
runtime chains, Blocks lowering, script execution, capability inference,
Action mounting, network fetching, or approvals. Remote sources must be
materialized by authorized host code before local installation.

```python
pack = await Agently.skills_executor.async_build_context_pack(
    task="Prepare the release review",
    skills=[contract["skill_id"]],
    include_references=True,
)
```

This method creates a temporary TaskContext and uses the same ContextReader
contracts as ordinary execution. `actionize_scripts=True` is ignored with a
diagnostic; Skill scripts remain descriptors until an owning Action or runtime
explicitly authorizes and executes them.

## Released execution convenience adapter

`agent.run_skills_task(...)` and `agent.async_run_skills_task(...)` remain
result-shaped adapters over one ordinary AgentExecution:

```python
compat = await agent.async_run_skills_task(
    "Review release candidate 3.2.0.",
    skills=[contract["skill_id"]],
    mode="required",
    output={"decision": (str, "GO or NO-GO", True)},
)
print(compat.execution.id, compat.output)
```

The adapter does not choose a `skills` route. The execution uses the same
`model_request` or explicit AgentTask strategy as any other request. New code
should prefer the direct AgentExecution API when it needs streams, metadata,
TaskContext diagnostics, retries, or lifecycle control.

## Context limits and progressive disclosure

Installing a Skill does not copy all of its resources into every prompt.
Required `SKILL.md` guidance is delivered first; resource indexes and explicit
references allow later bounded reads. When available context is too large, the
reader returns omissions and diagnostics plus refs for later reads. It never
pretends that a synthetic summary is the full source.

Use one or more bounded information blocks selected for the consumer and
phase. Keep full files and raw evidence in SkillLibrary, TaskWorkspace, or
RecordStore; put only the task-relevant package on the hot model path.

## Side effects

Skills describe how work should be done. Host code, ActionRuntime,
ExecutionResource, TaskWorkspace, RecordStore, TaskDAG, and TriggerFlow retain
their existing responsibilities. A Skill cannot silently grant filesystem,
network, MCP, credential, or process access.
