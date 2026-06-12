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

## Declare And Install

For normal Agent runtime, declare Skills on the Agent with `use_skills(...)`.
The Skills Executor treats repository/package selectors like Action candidates:
it records the source first, performs lightweight `SKILL.md` discovery during
planning, and installs the full Skill only when the planner selects or requires
it.

```python
agent.use_skills(
    [{"source": "anthropics/skills", "subpath": "skills/docx"}],
    mode="required",
)
```

Use `install_skills_pack(...)` for advanced pool management: prewarming, offline
mirrors, deterministic CI fixtures, or explicit registry maintenance.
`install_skills(...)` remains the authoring/smoke-test path for one local Skill
directory.

When you author a local Skill, keep it in a real standalone directory that
matches the standard layout. Do not build inline `SKILL.md` strings inside
business code and do not use root-level YAML manifests such as `skill.yaml`.
The application should pass the directory path to the executor:

```text
my-skill/
|-- SKILL.md
|-- scripts/
|-- references/
`-- assets/
```

```python
report = Agently.skills_executor.install_skills_pack(
    "anthropics/skills",
    fetch=True,
    subpath="skills/docx",
    trust_level="remote",
)
```

Remote installs clone the repository into Agently's local registry source cache,
copy standard `SKILL.md` packages into the registry, and record source URL, ref,
resolved commit, subpath, trust level, and checksums. Installing a remote Skill
never executes bundled scripts.

`install_skills(...)` copies a standard local Skill directory into the local
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

Use `use_skills(...)` to expose installed or remote Skills as route candidates.
The model sees concise decision cards first; full guidance and resources are
materialized only when the Skills route executes.

```python
agent = Agently.create_agent("ops-assistant")
agent.use_skills(["release-review"], mode="model_decision")
agent.use_skills([{"source": "anthropics/skills", "subpath": "skills/docx"}], mode="required")
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
model request. Host execution options such as `effort="react"` or configured
route options select multi-step strategies; Skills do not declare Agently
execution strategies through private frontmatter.
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

`output=` uses the same schema grammar as `.output(...)`; it is the
structured-output contract for the Skill run. It describes the business result
shape the Skill execution must produce, while `output_format=` controls whether
that result is carried as JSON, flat Markdown, hybrid output, XML-like field
envelopes, YAML literal documents, or automatic format selection. The older
`semantic_outputs=` argument is kept only as a deprecated compatibility alias
for Skills execution.

```python
execution = await agent.async_run_skills_task(
    "Write a release decision.",
    skills=["release-review"],
    mode="required",
    output={"decision": (str, "go or no-go", True)},
)
```

Agent prompt methods are also supported for explicit Skills execution. The
Skill run consumes the current turn prompt snapshot, uses rendered prompt text
as the task, and maps the `output` / `output_format` slots to `output` /
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

`set_agent_prompt(...)` values are inherited and kept for later executions.
Quick prompt values are frozen into the Skill run and then cleared from the
pending execution prompt. Explicit `output=` and
`output_format=` arguments override prompt-derived defaults.

`output_format=` selects how that model response is controlled. Leave it as
`"auto"` for ordinary Skill answers. Auto is structural: it chooses
`"xml_field"` for flat string-only dict schemas, `"hybrid"` for top-level
dicts that combine string fields with typed non-string fields, and `"json"` for
all-control schemas, all-complex schemas, and non-dict outputs. Use
explicit `"json"` for compact all-typed machine-readable results or downstream
JSON-only contracts. Use explicit `"xml_field"` when
flat string-only fields benefit from XML-like field boundaries, and explicit
`"hybrid"` for mixed long text plus typed fields. Use explicit `"yaml_literal"`
only when the task intentionally wants a YAML target
document.

```python
execution = await agent.async_run_skills_task(
    "Draft a release announcement as HTML.",
    skills=["release-review"],
    mode="required",
    output={"html": (str, "render-ready HTML", True)},
    output_format="xml_field",
)
```

For fixed required fields, prefer the third tuple element in the schema:

```python
output = {
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
- `skills.runtime_chain.*` when `effort="normal"` or `effort="max"` selects the
  built-in planner chain
- `skills.staged.*`, `skills.react.*`, and `block.*` events when a multi-step
  strategy is selected

Annotate direct Skills `stream_handler` callbacks with
`SkillRuntimeStreamHandler` from `agently.types.data`. If you are writing a
custom Skills effort strategy and call
`context.async_request_model(..., stream_handler=...)`, that model-stream
handler receives `StreamingData` and can be annotated with
`ModelStreamingHandler`. Both types are available from the package root:
`from agently import StreamingData, ModelStreamingHandler`.

`effort="fast"` uses the low-overhead single-shot path. `effort="normal"` runs
the full preflight -> research -> plan -> execute -> verify -> reflect ->
finalize chain. `effort="max"` uses the same chain with a larger retry budget
and is the planned hook for Dynamic Task escalation.

Use `agent.set_settings("effort_presets", {...})` when you need to override the
built-in profile mapping to strategy, model key, step budget, retry count, or
artifact inline limit:

```python
agent.set_settings("effort_presets", {
    "fast": {"strategy": "single_shot", "reason_key": "reason_fast", "step_budget": 1},
    "normal": {"strategy": "runtime_chain", "reason_key": "reason", "retry_count": 1},
})

execution = await agent.async_run_skills_task(
    "Draft a release decision.",
    skills=["release-review"],
    mode="required",
    effort="normal",
)
```

When Skills are selected by Agent auto-orchestration, pass the same effort
choice through `create_execution(options=...)`:

```python
from agently.types.options import ExecutionOptions, SkillsRouteOptions

execution = agent.input("Draft a release decision.").create_execution(
    options=ExecutionOptions(
        routes={"skills": SkillsRouteOptions(effort="normal")},
    )
)
```

For a fully custom action strategy, register an effort strategy handler on the
Skills Executor and invoke it through `effort=`. The handler receives the Agent
runtime context, selected Skills plan, resolved effort config, and output
format; it may request models, call Actions/MCP through the context, emit
runtime stream items, and return the final output.

The handler follows the `SkillsEffortStrategyHandler` protocol:

```python
def handler(
    *,
    context: SkillsExecutionContext,
    task: str,
    plan: SkillExecutionPlan,
    output_format: str | None = None,
    effort: str | None = None,
    effort_config: dict | None = None,
) -> Awaitable[Any] | Any: ...
```

The builtin handlers are registered through the same strategy table:
`single_shot`, `runtime_chain`, `staged`, and `react`. Use
`Agently.skills_executor.list_effort_strategies()` to inspect the available
strategy names. A custom handler may replace a builtin only with
`replace=True`; otherwise duplicate names fail closed. Builtin reference
implementations live under
`agently/builtins/plugins/SkillsExecutor/AgentlySkillsExecutor/modules/effort_strategies/`.

```python
async def audit_plus_strategy(*, context, task, plan, effort_config, **_):
    await context.async_emit_runtime_stream({
        "type": "skills.audit_plus.checkpoint",
        "action": "checkpoint",
    })
    return await context.async_request_model(
        prompt={
            "task": task,
            "selected_skills": plan["selected_skills"],
            "policy": effort_config,
        },
        model_key="verifier",
        output_schema={"decision": (str, "go / no-go", True)},
        output_format="json",
    )

Agently.skills_executor.register_effort_strategy(
    "audit_plus",
    audit_plus_strategy,
)

agent.set_settings("effort_presets", {
    "audit_plus": {"strategy": "audit_plus", "custom_budget": 7},
})

execution = await agent.async_run_skills_task(
    "Audit this release.",
    skills=["release-review"],
    mode="required",
    effort="audit_plus",
)
```

Skills runtime model calls use symbolic stage keys: `planner`, `research`,
`reason`, `executor`, `verifier`, `reflector`, and `finalizer`. If a key is not
mapped in `model_pool`, Agently leaves the request on the agent's inherited
model instead of sending the symbolic key as a provider model name.

When Skills are selected through Agent auto-orchestration, model field stream
items are bridged to stable paths like `skills.model.fields.<field_path>`.

## Context Packs for DAG Consumers

When a custom planner, Dynamic Task, or TaskDAG node needs complete Skill
context without forcing the whole Skills execution route, build a context pack.
The pack exposes the selected `SKILL.md` guidance, task-relevant references,
examples, optional assets, resource index metadata, citations, diagnostics, and
policy-gated action candidates under schema
`agently.skills.context_pack.v1`.

```python
pack = await agent.async_build_skills_context_pack(
    "Generate DeepSeek provider setup code.",
    skills=["model-setup"],
    intent="generate_code",
    include_examples="auto",
    include_references="auto",
    budget_chars=12000,
)
```

For DAG-shaped execution, reuse the Skills Executor resolver adapter instead of
creating a separate scheduler:

```python
from agently.core import TaskDAGExecutor

snapshot = await TaskDAGExecutor(
    Agently.skills_executor.task_dag_resolver()
).async_run({
    "graph_id": "skill-context-demo",
    "task_schema_version": "task_dag/v1",
    "tasks": [
        {
            "id": "skill_context",
            "kind": "skill",
            "inputs": {
                "task": "Generate provider setup code.",
                "skill_ids": ["model-setup"],
                "intent": "generate_code",
            },
        }
    ],
    "semantic_outputs": {"context": "skill_context"},
})
```

`include_public_lookup=True` and `actionize_scripts=True` remain opt-in host
policy operations. Public lookup requires `web_search: "allow"`. Script
Actionization requires `script_run: "allow"` or an approved PolicyApproval
decision; it only mounts an allowlisted shell Action candidate and does not run
the script.

Bundled scripts and resources are never executed just because a Skill is
installed. When a selected standard Skill describes a need for search, browse,
HTTP, Workspace file access, Python, shell/script execution, or MCP, Skills
Executor records structured `capability_needs` in the plan. The Skill still
does not grant capability. Before execution, Agently compares those needs with
host policy and can auto-load only the built-in capabilities explicitly marked
`allow`; `approval` and `off` fail closed with diagnostics.

```python
agent.configure_skill_capabilities(
    auto_load={
        "web_search": "allow",
        "web_browse": "allow",
        "workspace_write": "allow",
        "script_run": "approval",
        "shell": "approval",
        "mcp": "approval",
    },
    workspace_root="./.agently/tasks/research",
    search={
        "backend": "auto",
        "refresh_ddgs": "allow",
    },
)
agent.configure_policy_approval(handler="input_timeout_fail")
```

For search-oriented Skills, Agently mounts the framework Search package backed
by the `ddgs` Python package. Keep `ddgs` upgraded before real search runs:
`python -m pip install --upgrade ddgs`. The backend strategy is not fixed to one
provider; use `backend="auto"` by default, or configure any ddgs-supported
backend through host policy. Search treats backend-level "no results" as an
empty successful result and falls back through configured/default ddgs backends
when a selected backend returns no usable parsed result. If fallback recovers
after one or more backend failures, Search reports `status="partial_success"`
with `success=True` and backend diagnostics so the task can continue while the
operator still sees the degraded providers.

Workspace file operations are Workspace-owned. When an Agent has a Workspace,
SkillsExecutor exposes file actions through the Workspace file boundary before
falling back to `agent.enable_workspace_file_actions(...)`.

`approval` is handled by the framework-wide PolicyApproval handler, not by a
SkillsExecutor-local handler. The default is `input_timeout_fail`: interactive
CLI runs ask for input and fail after timeout, while non-interactive services
fail immediately. Tests and trusted local fixtures can use `auto_approve`.
Production services should register a handler that matches the service wrapping
the TriggerFlow execution, such as a database-backed pending approval record,
HTTP callback, webhook resume, SSE/WebSocket wait, or save-and-return interrupt
id. Use `fail_closed` when the host wants a pending diagnostic or a TriggerFlow
`policy_approval` interrupt instead of local input.

Skills Executor does not treat Skill frontmatter such as `mcp`, `mcpServers`,
`allow-scripts`, or Agently-specific `allowed-actions` as capability grants.
Public `compatibility` and `metadata` may contribute evidence to
`capability_needs`, but loading remains host-policy controlled.
Hosts that want model judgment in addition to deterministic Skill reading can
enable `skills.capability_discovery.model_assisted=True`; model-inferred needs
are still only evidence and must pass the same host policy gate.

The public Agent Skills `allowed-tools` field is experimental. If Agently
supports it, it can only restrict or pre-approve already-mounted host tools; it
does not mount new Actions, create MCP clients, enable shell/file access, or
synthesize missing backends.

## Acceptance Example

`examples/agent_auto_orchestration/19_remote_skills_weather_event_ops.py`
exercises the 4.1.3 remote-connector path end to end: public remote Skills are
declared with `agent.use_skills(...)`, a free weather MCP server is registered
through ActionRuntime, the model generates MCP action calls for real weather
observations, and Skills Executor lazily materializes the selected Skills before
running `effort="normal"`.

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
- `Agently.skills_executor.build_context_pack(...)`
- `Agently.skills_executor.task_dag_resolver(...)`
- `agent.build_skills_context_pack(...)`
- `agent.use_skills(...)`
- `agent.use_skills_packs(...)`
- `agent.resolve_skills_plan(...)`
- `agent.run_skills_task(...)`

`SkillContract` describes the installed standard Skill, Agently install
metadata, decision card, resource index, and checksums. It does not contain
framework-authored stage declarations.
