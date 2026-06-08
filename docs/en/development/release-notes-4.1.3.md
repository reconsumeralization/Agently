---
title: Agently 4.1.3 Release Notes
description: Agently 4.1.3 release notes from the 4.1.2 runtime foundation to the 4.1.3 AI application runtime line.
keywords: Agently, release notes, 4.1.3, Agent, Skills Executor, Dynamic Task, MCP
---

# Agently 4.1.3 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.md)

Agently 4.1.3 is the release where the 4.1.2 runtime foundation becomes a
coherent AI application runtime.

The goal is not to add another list of APIs. The goal is to let one Agent turn
connect model reasoning, Actions, remote Skills, MCP tools, Dynamic Task DAGs,
runtime streams, structured outputs, and companion coding-agent guidance through
one engineering path.

In 4.1.2, Agently established the runtime building blocks. In 4.1.3, those
blocks are connected into a default application path that can support real AI
services rather than prompt-only demos.

## Core Outcome

Agently can now act as the execution substrate for production-grade AI service
backends:

```text
business input
  -> Agent
  -> candidate Actions / Skills / Dynamic Task
  -> model-guided planning and execution
  -> ActionRuntime / ExecutionEnvironment / TriggerFlow
  -> streamed process events
  -> structured business output
```

This matters because real AI services need more than text generation. They need
stable output contracts, observable tool calls, external-system boundaries,
recoverable execution, and guidance that keeps human developers and coding
agents on the same recommended path.

## Agent As The Default Runtime Entry

`agent.start()` is now the default user-layer entrypoint for a candidate-aware
Agent turn. The caller still receives the business result, while the Agent can
route through ordinary model response, Actions, Skills Executor, or Dynamic
Task when those candidates were explicitly declared.

```python
result = (
    agent
    .use_actions([lookup_customer, fetch_contract, notify_owner])
    .use_skills(
        [{"source": "anthropics/skills", "subpath": "skills/docx"}],
        mode="model_decision",
    )
    .use_dynamic_task(mode="auto", max_tasks=8)
    .input({"customer_id": "C-1024", "ticket": "payment failure"})
    .output({
        "summary": (str, "business summary", True),
        "risk_level": (str, "low / medium / high", True),
        "next_actions": ([str], "recommended actions", True),
    })
    .start()
)
```

Business value: application code can describe the capabilities available to one
business turn, then let the runtime choose and execute the right path without
turning the service into hand-written prompt glue.

## Execution Objects And Process Streams

For services that need rich stage-by-stage feedback, the same Agent turn can be
created as an execution object. The primary value is not just logging or
diagnostics; it is exposing concrete runtime information from any stage to the
caller, UI, or downstream service while the work is still running.

```python
execution = (
    agent
    .use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)
    .input({"ticket": "T-42"})
    .create_execution()
)

async for item in execution.get_async_generator(type="instant"):
    send_to_ui(item.path, item.value)

data = await execution.async_get_data()
meta = await execution.async_get_meta()
```

Business value: frontends no longer need to stare at a black-box loading state.
They can show route decisions, research findings, graph readiness, task starts,
action calls, tool results, selected field deltas, approval or blocked states,
intermediate artifacts, and final structured outputs. Logs and diagnostics are
important secondary consumers of the same stream.

## Skills As Runtime Capabilities

`agent.use_skills(...)` is now the recommended Agent-level declaration surface
for Skills. Its default mode is `model_decision`: the planner decides whether
and which declared Skills should be used. Application code declares candidate
sources; the Skills Executor handles lightweight discovery, planner selection,
on-demand materialization, capability mounting, diagnostics, and execution.

```python
agent.use_skills(
    [
        {"source": "GarethManning/education-agent-skills"},
        {"source": "anthropics/skills", "subpath": "skills/docx"},
        {"source": "anthropics/skills", "subpath": "skills/pptx"},
        {"source": "anthropics/skills", "subpath": "skills/xlsx"},
    ],
    mode="model_decision",
)

execution = await agent.async_run_skills_task(
    "Create a four-week B1 business English course package.",
    effort="normal",
    output={
        "course_plan": (dict, "course goals, weekly structure, and lesson sequence", True),
        "teacher_guide": (str, "teacher-facing guide summary", True),
        "student_handout_plan": (str, "student material plan", True),
        "progress_tracker": ([str], "progress tracking columns and checkpoints", True),
    },
)
```

Business value: Skills become reusable runtime capabilities, not inline prompt
snippets inside business code. Teams can point at public or private Skill
repositories, keep local authoring for development, and let the runtime install
only what the task actually needs.

## MCP And Script Capabilities Through The Runtime

Skills that declare MCP, shell, or script capabilities are resolved by the
Skills Executor and mounted through the existing ActionRuntime and
ExecutionEnvironment boundaries. The Skill does not create a second tool system.

```python
agent.use_skills(
    [{"source": "owner/skills-with-mcp", "trust_level": "remote"}],
    mode="required",
    auto_allow=False,
)
```

If the selected Skill declares HTTP MCP, it is mounted automatically during
Skill execution. If it declares stdio, `npx`, local-command MCP, shell, or
script execution, the runtime requires explicit approval or `auto_allow=True`:

```python
agent.use_skills(
    [{"source": "owner/skills-with-local-mcp", "auto_allow": True}],
    mode="required",
)
```

Direct MCP registration remains available when the MCP service is an
application-owned capability rather than a Skill-declared capability:

```python
await agent.use_mcp({
    "mcpServers": {
        "market_data": {
            "command": "npx",
            "args": ["-y", "octagon-mcp"],
        }
    }
})
```

Application-owned HTTP MCP services can also be used directly:

```python
await agent.use_mcp(
    "https://example.com/mcp",
    headers={"Authorization": "Bearer ..."},
)
```

Business value: external tools, local commands, and MCP services are observable,
policy-controlled runtime capabilities. High-risk local execution requires
explicit approval or `auto_allow=True`; safe missing pure-computation helpers
can be synthesized as sandboxed Python actions, while business-system
capabilities fail closed unless a real Action or connector is mounted.

## Effort-Aware Skills Planning

Skills execution now supports runtime effort levels and custom effort strategy
handlers.

```python
execution = await agent.async_run_skills_task(
    "Prepare release readiness evidence and decide go/no-go.",
    skills=["release-readiness-reviewer"],
    mode="required",
    effort="normal",
    output={
        "decision": (str, "go / no-go", True),
        "blocking_risks": ([str], "release blocking risks", True),
        "required_followups": ([str], "follow-up actions", True),
    },
)
```

Effort semantics:

- `fast`: compresses planning and review where possible while still completing
  the task.
- `normal`: runs the full chain of preflight, research/context, plan, execute,
  verify, reflect/retry, and finalize.
- `max`: uses higher budgets, stronger verification, retry loops, and can
  escalate complex work toward Dynamic Task DAG execution.

Teams can register their own strategy:

```python
Agently.skills_executor.register_effort_strategy("audit_plus", handler)

execution = await agent.async_run_skills_task(
    "Run a regulated readiness review.",
    effort="audit_plus",
)
```

Business value: teams can trade latency, cost, and assurance explicitly. The
same Skill can run quickly for routine work, thoroughly for important business
decisions, or through a custom organization-specific strategy.

## Dynamic Task And TriggerFlow As The Execution Backbone

Dynamic Task remains the right surface for complex model-generated or
application-submitted DAGs. In 4.1.3, Agent execution can route into Dynamic
Task and preserve streamed field deltas under stable paths.

```python
execution = (
    agent
    .use_dynamic_task(mode="auto", max_tasks=8)
    .input("Research this company and produce an investment memo.")
    .output({
        "thesis": (str, "investment thesis", True),
        "risks": ([str], "major risks", True),
        "evidence": ([str], "supporting evidence", True),
    })
    .create_execution()
)

async for item in execution.get_async_generator(type="instant"):
    if item.delta:
        print(item.path, item.delta)
```

Business value: complex work can be decomposed, streamed, inspected, and
recovered through the same runtime instead of being compressed into one prompt
or rewritten as a separate workflow engine.

`max_tasks` is a planning guardrail, not an instruction to run forever. When it
is omitted, Agently does not impose an explicit task-count limit beyond any
configured planner setting; the planner still generates a finite DAG, validation
and retry limits still apply, and execution ends when that DAG completes or
fails.

## Model Pool And Stage Routing

Agently 4.1.3 supports three-layer model resolution:

```text
business model key
  -> model_pool concrete model name
  -> key_pool_strategy key id
  -> key_pool API key
```

This separates model aliases such as `ollama-qwen2.5` or `deepseek-v4` from
provider model names and API keys.

```python
agent.set_settings("model_pool", {
    "ollama-qwen2.5": "qwen2.5:7b",
    "deepseek-v4": "deepseek-chat",
})
agent.set_settings("key_pool", {
    "local": "ollama",
    "deepseek-main": "${ENV.DEEPSEEK_API_KEY}",
    "deepseek-backup": "${ENV.DEEPSEEK_BACKUP_API_KEY}",
})
agent.set_settings("key_pool_strategy", {
    "qwen2.5:7b": {"mode": "fixed", "pool": ["local"]},
    "deepseek-chat": {"mode": "round_robin", "pool": ["deepseek-main", "deepseek-backup"]},
})
```

Ordinary Agent turns can switch the active model with `activate_model(...)`:

```python
result = (
    agent
    .activate_model("ollama-qwen2.5")
    .input("Summarize this incident.")
    .output({"summary": (str, "incident summary", True)})
    .start()
)
```

For one-off calls, `create_request(model_key=...)` still overrides the active
Agent model:

```python
result = agent.create_request(model_key="deepseek-v4").input("Draft the customer reply.").start()
```

Skills planning and execution stages use the same model-key layer rather than
hard-coded provider model names:

```python
agent.set_settings("skills.runtime.stage_model_keys", {
    "planner": "deepseek-v4",
    "research": "deepseek-v4",
    "executor": "ollama-qwen2.5",
    "verifier": "deepseek-v4",
    "finalizer": "deepseek-v4",
})
```

API key switching is automatic at request time according to
`key_pool_strategy`: `fixed`, `random`, `round_robin`, or `least_used`.
4.1.3 does not automatically retry a failed provider request with another key
after an auth, quota, or billing error. Those failures are surfaced so the
application can decide whether switching credentials is safe for that business
operation.

Business value: services can route cheap, fast, or stronger models to different
runtime stages without changing business code. Planning, research, execution,
verification, reflection, and finalization can use the right model for the job.

## Recommended Service Patterns

4.1.3 is especially suited for:

- enterprise operations services: ticket triage, incident response, renewal
  risk, sales research;
- research and reporting services: market analysis, policy summaries, OSS
  evaluation, investment memos;
- professional artifact workflows: docx, xlsx, pptx, pdf packages from a single
  structured run;
- external-tool Agents: MCP, databases, browsers, calculators, local execution,
  and business connectors;
- long-running AI services with frontend process visualization.

The key shift is that Agently now gives these systems one runtime mental model:

```text
declare capabilities
plan with the model
execute through owned runtime boundaries
stream the process
return structured business results
```

## Compatibility Notes

- The package version is `4.1.3`.
- The release manifest is `compatibility/releases/4.1.3.json`.
- Agently 4.1.3 recommends `agently-devtools >=0.1.5,<0.2.0`.
- Agently-Skills uses authoring protocol `agently-skills.authoring.v2` and
  standard `SKILL.md` packages.
- `semantic_outputs=` for Skills execution is retained as a deprecated
  compatibility alias. New code should use `output=`.
