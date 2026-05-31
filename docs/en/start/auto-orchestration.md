# Agent Auto-Orchestration

Agently 4.1.3 makes `agent.start()` the default user-layer entrypoint for an
Agent turn. It keeps returning the business result, while the Agent can route
through ordinary model response, Actions, Skills Executor, or Dynamic Task when
those candidates were explicitly injected.

```python
result = (
    agent
    .use_actions([market_data_action])
    .use_skills_packs(["equity-research"])
    .use_dynamic_task(mode="auto", max_tasks=8)
    .input("Review this renewal risk.")
    .output({"answer": (str, "final answer", True)})
    .start()
)
```

Candidate injection is the boundary. If no Actions, Skills, Skills Packs, or
Dynamic Task candidates are registered, `agent.start()` remains an ordinary
model request.

Accepted development-line routing is candidate-driven and deterministic-first:
submitted Dynamic Task candidates take precedence and required Skills
candidates run through the Skills route. When several optional candidates are
present, such as auto Dynamic Task, model-decision Skills, and ordinary Actions,
the model chooses the route by default. If there is only one optional candidate,
that route is selected directly.

The public Agent API stays in core, but route planning and execution are owned
by the active `AgentOrchestrator` plugin through the `AgentOrchestrator`
protocol. This keeps Skills, Dynamic Task, and future route implementations
replaceable without teaching core about builtin plugin internals.

## Execution Object

Use `agent.create_execution()` when the caller needs route diagnostics, multiple
result views, or process streaming:

```python
execution = (
    agent
    .use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)
    .input("Run the reviewed graph.")
    .create_execution()
)

async for item in execution.get_async_generator(type="instant"):
    if item.is_complete:
        print(item.path, item.value)

data = await execution.async_get_data()
meta = await execution.async_get_meta()
```

The execution object follows the same consumption style as model responses:
`get_data`, `get_text`, `get_meta`, `get_generator`, and async equivalents.

`create_execution()` defaults to `mode="one_turn"`, which preserves the
ordinary one-turn Agent behavior. When a developer-owned loop or future
AgentTaskLoop needs one bounded step, use `mode="task_step"` with explicit
lineage and limits:

```python
execution = agent.input("Try one bounded fix step.").create_execution(
    mode="task_step",
    lineage={
        "task_id": "issue-123",
        "iteration_id": "iter-2",
        "step_id": "execute-fix",
        "parent_execution_id": "exec-prev",
    },
    limits={"max_model_requests": 3},
)
```

`mode="task_step"` is still one Agent execution, not a multi-turn loop. It adds
stable lineage, route metadata, diagnostics, and shared model-request budget
counting across direct model routes, Dynamic Task model tasks, and Skills model
stages. Use `None` for an unlimited budget; `-1` is accepted as a compatibility
spelling but should not be used in new examples.

If a task-step exceeds its model-request budget, Agently raises
`AgentExecutionLimitExceeded` from `agently.core.AgentExecution`. The execution
meta remains inspectable and records `status="blocked"` plus the limit event in
`diagnostics`.

`async_get_meta()` includes `execution_mode`, `lineage`, `limits`, `route`,
`route_plan`, `logs`, `diagnostics`, and `workspace_refs`. `logs` is the
route-independent place to inspect runtime facts such as model response ids,
ActionRuntime action records, and artifact refs:

```python
meta = await execution.async_get_meta()
meta["route"]["selected_route"]
meta["logs"]["model_response_ids"]
meta["logs"]["action_logs"]
meta["logs"]["artifact_refs"]
```

When a `model_request` route uses Actions, the execution exposes the action
records through both meta and stream events such as `actions.<action_id>`.
Hosts that need to persist business evidence should read the framework action
record or artifact, then explicitly write the selected observation to
Workspace. Do not ask the model to copy raw action stdout just to make the host
able to store it.

Every process-stream item also receives correlation metadata:

```python
item.meta["execution_id"]
item.meta["execution_mode"]
item.meta["lineage"]["task_id"]
```

When `agent.use_workspace(...)` is configured before `create_execution()`, the
execution receives that Workspace binding. AgentExecution still does not decide
what becomes memory automatically; persist explicitly from the execution side:

```python
workspace_record = await execution.async_record_workspace(
    collection="observations",
    kind="agent_execution_observation",
    content={"result": data},
    checkpoint=True,
)
```

This writes through the existing generic Workspace APIs and updates
`meta["workspace_refs"]` with the record and checkpoint ids. Workspace remains
the durable substrate and does not need to know AgentExecution semantics. Call
`workspace.build_context(...)` for the next step.

For development diagnostics, attach an EventCenter observation hook or
temporarily enable console details:

```python
Agently.event_center.register_hook(print, event_types=None, hook_name="debug")
agent.set_settings("debug", "detail")
```

Use this only while debugging route selection, model requests, ActionRuntime, or
Workspace persistence. Remove debug hooks/settings from examples and production
snippets once the problem is understood.

## Submitted Dynamic Task Input

Submitted Dynamic Task DAGs keep using DAG runtime placeholders such as
`${INIT.ticket}` and `${DEPS.lookup}` inside task `inputs`. Under an Agent
route, the graph input source is resolved in this order:

```text
use_dynamic_task(graph_input=...)
> the execution prompt snapshot input slot
> {"target": task_target}
```

This lets ordinary Agent prompt code feed a submitted DAG without inventing a
second mapping surface:

```python
execution = (
    agent
    .use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)
    .input({"ticket": "TICKET-OK"})
    .create_execution()
)
```

The prompt snapshot is captured by `create_execution()`. Later changes to
`agent.input(...)` do not alter an already-created execution. Use
`graph_input=...` when the DAG input must be different from the Agent prompt
input, or when you want that precedence to be explicit.

## Skills Semantics

`agent.use_skills(...)` and `agent.use_skills_packs(...)` register route
candidates. They no longer mean "inject full Skill guidance into the ordinary
model request" by default. Full Skill guidance belongs to a Skills route that
actually plans or executes the Skill. If the route does not select Skills, the
ordinary request receives only safe capability summaries.

Use `agent.run_skills_task(...)` when a caller must force Skills execution.

## Process Stream

Agent execution stream items follow the familiar instant-stream shape:

```python
item.path
item.value
item.delta
item.event_type
item.is_complete
item.route
item.stage_id
item.task_id
item.action_id
item.graph_id
```

Executor routes bridge TriggerFlow runtime stream and ModelRequest instant
checkpoints so services can stream route decisions, plan/graph readiness,
task/action progress, selected model field deltas, and final semantic outputs.
If a TriggerFlow-backed route fails, the Agent execution stream is closed and
the original error is raised to the consumer instead of leaving
`get_async_generator(...)` waiting for more items.

For Dynamic Task model nodes, structured output fields stream under stable paths:

```python
async for item in execution.get_async_generator(type="instant"):
    if item.path == "task_dag.tasks.reply.fields.reply" and item.delta:
        print(item.delta, end="", flush=True)
```

This preserves model-response `instant` semantics while keeping process-stream
paths owned by the Agent execution route.
