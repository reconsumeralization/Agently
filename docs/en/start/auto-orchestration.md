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

## Submitted Dynamic Task Input

Submitted Dynamic Task DAGs keep using DAG runtime placeholders such as
`${INPUT.ticket}` and `${DEPS.lookup}` inside task `inputs`. Under an Agent
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
