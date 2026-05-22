---
title: Skills Executor
description: Planner-selected declarative behavior loops exposed through Agent APIs.
keywords: Agently, Skills Executor, skills, behavior loop, run_skills_task, use_skills
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

- `Agently.skills_executor.install_skills(...)`, `install_skills_pack(...)`,
  `list_skills()`, `list_skills_packs()`, `inspect_skills()`,
  `inspect_skills_pack()`, `remove_skills()`, and `remove_skills_pack()`
- `agent.use_skills(...)` for optional model-decision skill card disclosure
- bounded primary `SKILL.md` guidance disclosure for selected candidate skills
- `agent.resolve_skills_plan(...)` for producing a `SkillExecutionPlan`
- `agent.run_skills_task(...)` for explicit skill task execution
- SkillCard composition metadata such as `stage_roles`, `consumes`,
  `produces`, `artifact_types`, `side_effects`, `required_capabilities`,
  `complements`, and `failure_modes`
- semantic output contracts through `semantic_outputs`, so realcase tests
  validate deliverable role/type coverage instead of hard-coded filenames
- model-composed multi-skill planning through `planner_mode="model"` with a
  bounded evaluate/repair loop controlled by `planner_max_revisions`
- declarative `action`, `model`, `validate`, and `emit` stage handling
- Dynamic Task DAG execution underneath `run_skills_task(...)`, so
  `SkillExecution.close_snapshot` preserves the compiled task graph result
  alongside skill/action logs

The current implementation keeps model-owned planning behind the
`SkillExecutionPlan` contract. The planner can select and combine multiple
candidate skills, describe stage switching, approval gates, fallback paths,
intermediate artifacts, side-effect boundaries, and expected semantic outputs.

The retained framework layering is:

```text
core/SkillsExecutor.py
  -> active SkillsExecutor plugin
  -> builtins/plugins/SkillsExecutor/AgentlySkillsExecutor/
       - AgentlySkillsExecutor.py
       - registry.py
       - planner.py
       - executor.py
  -> types/plugins/SkillsExecutor.py
       - SkillsExecutor
       - SkillsPlanningContext / SkillsExecutionContext / SkillsRuntimeContext
  -> builtins/agent_extensions/SkillsExtension/
       - SkillsExtension.py
       - _SkillsContext.py
```

The plugin does not receive the full Agent object. The Agent component builds a
`SkillsRuntimeContext` adapter for model planning, settings lookup, action
availability, and action execution.

`Agently.skills_executor` is the only global facade for this development-line
feature. The feature has not shipped yet, so no `Agently.skills` compatibility
alias is retained.

## User Mental Model

A Skill is not a `skill.run()` function and it is not an `ActionExecutor`.

```text
Agent API
  -> skill cards and policy filtering
  -> SkillExecutionPlan
  -> Dynamic Task DAG
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

The same behavior works with Agently's chain style. Installed skills behave like
model-decision capabilities, similar to actions/tools: they are disclosed to the
request and the model decides whether they fit.

```python
result = (
    agent
    .use_skills(["release-checklist"])
    .input("Should this release be blocked?")
    .instruct("Use installed skills only if they fit the task.")
    .output({"reply": (str,)})
    .start()
)
```

## Required Skill Task

Use `run_skills_task(...)` when a task must be handled through a skill loop.

```python
execution = await agent.async_run_skills_task(
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

For combo Skill Packs, pass expected deliverables as a semantic contract and
ask the model planner to compose the behavior loop:

```python
execution = await agent.async_run_skills_task(
    "Design a 4-week B1 business English course package.",
    skills=[
        "learner-profile-intake",
        "backwards-design-unit-planner",
        "retrieval-practice-generator",
        "formative-assessment-generator",
        "docx",
        "pdf",
        "pptx",
        "xlsx",
    ],
    mode="model_decision",
    semantic_outputs=[
        "course_plan.json",
        "teacher_guide.docx",
        "student_handout.pdf",
        "lesson_slides.pptx",
        "progress_tracker.xlsx",
        "skill_trace.json",
    ],
    planner_mode="model",
    planner_max_revisions=2,
)

print(execution.plan["selected_skills"])
print(execution.plan["stage_plan"])
print(execution.plan["planner_evaluation"])
print(execution.close_snapshot["task_dag"]["semantic_outputs"])
```

`semantic_outputs` accepts file-like names for convenience or explicit
deliverable dictionaries. The executor normalizes them into roles and artifact
types, so an output can pass as long as the plan covers the required meaning.
Filename normalization is an executor concern; it is not the user's contract.

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

## Composition Metadata

Real Skill Packs should describe how they compose with other Skills. These
fields are preserved in `SkillCard` and disclosed to the planner.

```yaml
card:
  stage_roles: [intake, action, validation]
  consumes:
    - role: task_request
      type: text
  produces:
    - role: release_note
      type: json
  artifact_types: [json]
  side_effects:
    - kind: local_record
      policy: allowed
  required_capabilities: [record_release_note]
  complements: [repo-review]
  failure_modes: [missing_action]
```

## Boundaries

- Scripts and helpers must run through controlled Actions, not arbitrary Skill
  Python handlers.
- Third-party Skill scripts are installed as inert assets by default, but the
  executor owns capability resolution. It should try controlled replacements
  such as built-in Actions, sandbox-backed Bash/Python/Node actions, MCP/API
  bindings, or declared fallback branches before blocking the run.
- If a Skill requires a Bash/shell-style action and no action is bound, the
  executor may auto-bind a controlled Bash sandbox with the configured command
  allowlist and workspace boundary. It must not silently run arbitrary package
  scripts.
- If no controlled replacement is available, the execution should fail closed
  with a natural-language `user_message` and resolution suggestions instead of
  leaving application code to interpret internal error codes.
- Guidance disclosure is prompt context, not code execution.
- MCP, browser, sandbox, process, and credential resources belong to Execution
  Environment.
- Long-running workflow behavior should be represented by TriggerFlow-backed
  skill execution, not hidden inside a Skill package.

## Tested External Skill Packages

`examples/skills_executor/02_deepseek_external_skill_cards.py` installs and tests:

- `../Agently-Skills/skills/agently-runtime`
- `anthropics/skills/skills/xlsx`
- `anthropics/skills/skills/webapp-testing`

The example verifies that DeepSeek receives the selected skill card and bounded
primary guidance in `model_decision` mode, while package scripts remain inert
assets unless the app binds them through Actions.

`examples/skills_executor/04_dynamic_todo_triggerflow_realcase.py` is the
diagnostic realcase variant. It installs `Agently-Skills`, exposes
`agently-playbook`, `agently-request`, and `agently-triggerflow` to DeepSeek
through `agent.use_skills(...)`, then asks DeepSeek to generate both the Todo
DAG and a complete Python TriggerFlow executor module. The prompt intentionally
does not spell out TriggerFlow API details; the host script evaluates whether
the model-generated module used real Agently APIs and whether it ran. The
diagnostic prints pass/fail and exits successfully by default for interactive
use; pass `--strict-exit` to make evaluator failure exit nonzero in CI.

`examples/skills_executor/03_stock_research_business_minimal.py` is the
business-facing minimal example. It installs a local Skill Pack with
`Agently.skills_executor.install_skills_pack(..., name="equity-research-demo")`,
attaches the pack through `agent.use_skills_packs(...)`, then sends a stock
research task to DeepSeek or local Ollama. Before model analysis, the executor
runs a controlled `fetch_equity_market_data` Action stage to retrieve current
public quote data from Stooq's CSV endpoint. The provider timestamps may be
delayed and are not exchange-direct realtime ticks, but the result is fetched
at runtime rather than supplied as sample data. Transient 503/504 and timeout
errors are retried; if the provider remains unavailable, the Action degrades to
the last successful local quote cache and marks the data status as degraded, or
returns the affected ticker as unavailable when no cache exists.

`examples/skills_executor/05_combo_skillpack_diagnostics.py` is the combo Skill
Pack benchmark. It validates realcase orchestration pressure points across:

- education course pack generation
- stock research pack generation
- travel planning with approval before external writes
- research report to spreadsheet, document, deck, and PDF
- web app acceptance testing evidence packs

The combo benchmark uses real local `SKILL.md` packages when available and
skips missing public sources rather than substituting mock skills. It can fetch
the referenced public repositories with `--fetch-missing`. The benchmark now
runs through `agent.run_skills_task(..., semantic_outputs=...,
planner_mode="model")`. The model receives optional SkillCards plus bounded
primary guidance; the executor turns the model-composed plan into a Dynamic
Task DAG; the host evaluator checks skill selection, stage switching,
intermediate artifacts, side-effect boundaries, approval gates, fallbacks, and
semantic output coverage.

The full DeepSeek benchmark adds a content-level Agently model judge after the
deterministic gate. The judge output schema puts evidence and concise rationale
before each rule's final boolean, and puts the overall `passes` boolean last, so
the final judgment is conditioned by the preceding structured facts.

The benchmark is intentionally a plan/contract and Dynamic Task execution
acceptance gate. It does not claim that third-party document skills have
executed arbitrary package scripts or written final `.docx`, `.pdf`, `.pptx`,
or `.xlsx` files. Those effects must be supplied by controlled Actions and
Execution Environment bindings.

`examples/skills_executor/06_executable_education_course_pack.py` is the first
execution-grade benchmark. It uses the same external Skill Pack planning path,
then runs a local dependency-installer Skill through the Skills Executor. That
Skill calls a controlled `ensure_python_packages` Action to install missing
artifact-writer libraries (`python-docx`, `openpyxl`, `python-pptx`,
`reportlab`, `pypdf`) before file generation. Missing local libraries are not a
fallback condition; they must be repaired by an Action or the execution fails
closed. The benchmark then writes real `docx`, `pdf`, `pptx`, `xlsx`, and
`json` artifacts, runs deterministic file checks, and uses an
output-controlled Agently model judge for semantic content judgment.

The same five combo cases are also pytest benchmarks:

```bash
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_combo_benchmarks.py

AGENTLY_RUN_SKILLS_BENCHMARKS=1 \
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_combo_benchmarks.py -m skills_benchmark

AGENTLY_RUN_SKILLS_REAL_EXECUTION=1 \
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_real_execution_benchmarks.py -m skills_real_execution
```

The first command validates source discovery and installability without model
calls. The second command is the full DeepSeek-backed planning benchmark. The
third command is the real execution benchmark and should be treated as the
acceptance gate for artifact-producing Skills.

## See Also

- [Coding Agents](coding-agents.md)
- [Action Runtime](../actions/action-runtime.md)
- [Execution Environment](../actions/execution-environment.md)
- [TriggerFlow Overview](../triggerflow/overview.md)
