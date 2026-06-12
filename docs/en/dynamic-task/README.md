# TaskDAG And Dynamic Task

`TaskDAG` is Agently's foundation DAG capability for model-generated or
app-generated task graphs. It owns the graph data contract, planner, validator,
resolver, executor, handler binding, dependency results, semantic outputs, and
runtime placeholders. TriggerFlow remains the lower-level execution substrate.

`DynamicTask` is the current compatibility and convenience facade over this DAG
substrate. It is useful when ordinary app code wants one compact entrypoint, but
it is not a second recommended task lifecycle beside `AgentExecution`.

```python
task = Agently.create_dynamic_task(target="review policy")
result = await task.async_start()
```

When the caller already has a plan, pass the `TaskDAG` directly and skip model
planning:

```python
async def local_handler(context):
    return {
        "task_id": context.task.id,
        "deps": dict(context.dependency_results),
    }

task = Agently.create_dynamic_task(
    target="review policy",
    plan={
        "graph_id": "review",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {"id": "extract", "kind": "local", "binding": "local_handler"},
            {
                "id": "final",
                "kind": "local",
                "binding": "local_handler",
                "depends_on": ["extract"],
            },
        ],
        "semantic_outputs": {"final": "final"},
    },
    handlers={"local_handler": local_handler},
)
snapshot = await task.async_start(timeout=10)
```

Advanced callers can decompose the same DAG path into independent modules,
customize them, and write the result back onto an `AgentExecution`:

```python
from agently.builtins.plugins import AgentlyTaskDAGPlanner
from agently.core import TaskDAGResolver, TaskDAGValidator

handlers = {
    "fetch_handler": fetch_handler,
    "analyze_handler": analyze_handler,
    "render_handler": render_handler,
}
resolver = TaskDAGResolver(handlers)
validator = TaskDAGValidator(resolver)
planner = AgentlyTaskDAGPlanner(validator=validator)

graph = await planner.async_plan(planner_agent, {"target": goal})
validator.validate(graph, strict_schema_version=True)

execution = agent.create_execution()
execution.input({"goal": goal})
execution.use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)
result = await execution.async_start()
```

Submitted DAG `inputs` may reference runtime data with placeholders. A whole
string placeholder preserves the original value type; embedded placeholders are
rendered into the surrounding string. Slot names are case-insensitive, but docs
use uppercase:

```python
plan = {
    "graph_id": "review",
    "task_schema_version": "task_dag/v1",
    "tasks": [
        {"id": "lookup", "kind": "local", "binding": "local_handler"},
        {
            "id": "final",
            "kind": "local",
            "binding": "local_handler",
            "depends_on": ["lookup"],
            "inputs": {
                "account": "${INIT.account}",
                "ticket": "${DEPS.lookup.ticket}",
                "summary": "Ticket ${STATE.task_results.lookup.ticket.id} for ${INIT.account}",
            },
        },
    ],
}
```

`${INIT}` points at the submitted graph input / initial execution input.
`${DEPS...}` points at completed dependency results. `${STATE...}` reads
execution state, for example `${STATE.task_results.lookup}`. `${TRIGGER...}`
points at the raw TriggerFlow trigger payload (`data.value`) and is mainly for
advanced debugging or executor-level integrations. Missing runtime paths fail
closed during task execution instead of staying as unresolved strings.

When a submitted DAG runs through `agent.use_dynamic_task(...).create_execution()`,
`${INIT...}` first reads an explicit `use_dynamic_task(graph_input=...)` value.
If that argument is omitted, it reads the execution prompt snapshot `input` slot
captured by `create_execution()`. Only when neither source exists does the Agent
route fall back to `{"target": task_target}`.

Use `execution.use_dynamic_task(...)` after `agent.create_execution()` when the
DAG candidate should be execution-local. That form uses the same graph input
rules but does not mutate the Agent-level DAG candidate pool.

If `create_dynamic_task(..., output_schema=..., ensure_keys=...)` supplies the
frontstage contract for a semantic-output model node, that host contract wins
over an incompatible planner-chosen node format. For multi-field structured
contracts, a planner's `inputs.output_format="flat_markdown"` is coerced back to
`auto` so the output parser can choose a compatible structured format.

Submitted plans can also be kept as YAML or JSON config artifacts. Load the
config into `TaskDAG`, then pass it through the same facade:

```python
from agently.core import TaskDAG

graph = TaskDAG.from_yaml("examples/dynamic_task/config_policy_review.yaml")
task = Agently.create_dynamic_task(
    target="review policy",
    plan=graph,
    handlers={"local_handler": local_handler},
)
snapshot = await task.async_run(graph_input={"doc": "policy"}, timeout=10)
```

`TaskDAG.from_json(...)` accepts a file path or raw JSON/JSON5 content. Both
`from_yaml(...)` and `from_json(...)` support `task_dag_key_path="plans.review"`
for selecting one DAG inside a larger config file. Use `graph.get_yaml(path)`
or `graph.get_json(path)` to export a normalized graph.

Agent instances expose the same compatibility facade:

```python
task = agent.create_dynamic_task(target="review policy")
```

Agent prompt methods are configuration. `agent.create_dynamic_task()` consumes
the same prompt snapshot as `agent.start()` / `agent.create_execution()`:

```python
task = (
    agent
    .info({"customer": "Acme"})
    .instruct("Focus on renewal risk and account-team actions.")
    .input({"account": "Acme", "ticket": "T-42"})
    .output({
        "summary": (str, "risk summary", True),
        "risks": ([str], "risk bullets", True),
    }, format="json")
    .create_dynamic_task()
)
```

The prompt snapshot is rendered through the normal Prompt generator to become
the DAG target. The `output` slot becomes the facade-level
`output_schema`, and `output_format` becomes the default model-task format.
`set_agent_prompt(...)` / `always=True` values are inherited. In a quick prompt
chain, execution prompt values are held on the AgentExecution draft and frozen
into the new task; use `agent.create_request(...)` / `agent.request` only for
the lower-level request-builder surface. Explicit
`create_dynamic_task(target=..., output_schema=..., output_format=...)`
arguments override prompt-derived defaults.

For model tasks, use Agently's request output pipeline instead of parsing model
text in handlers or examples. `output_schema` applies to semantic output model
tasks; node-level `inputs.output_schema` can override it for a specific model
task. Each model task may also set `inputs.output_format`:

- `json`: compact machine-control outputs, action arguments, routing flags,
  numeric or boolean facts, model judges, dense nested arrays/objects, and
  strict extraction.
- `flat_markdown`: explicit compatibility mode for legacy section-header
  prompts.
- `hybrid`: default auto target, or explicit mode, for long prose/code fields
  mixed with typed list/object/boolean/number fields.
- `xml_field`: default auto target, or explicit mode, for flat string-only
  dict schemas. It uses Agently's custom boundary parser, not strict XML.
- `yaml_literal`: explicit YAML target document for teams that prefer YAML and
  can tolerate indentation sensitivity.
- `auto`: structural schema-driven selection when retry latency is acceptable.

```python
task = Agently.create_dynamic_task(
    target="write an incident briefing",
    output_schema={
        "brief": (str, "customer-facing briefing", True),
        "next_update": (str, "next update timing", True),
    },
)
snapshot = await task.async_start(timeout=120)
_, output = next(iter(snapshot["semantic_outputs"].items()))
brief = output["result"]["brief"]
```

For submitted DAGs, put the task-specific strategy on the model task itself:

```python
{
    "id": "render_html",
    "kind": "model",
    "inputs": {
        "output_schema": {"html": (str, "render-ready HTML", True)},
        "output_format": "flat_markdown",
    },
}
```

Submitted DAG placeholders use the same uppercase naming style as Prompt
references, but they are a TriggerFlow runtime namespace rather than Prompt
slot references. `${INIT.foo}` points at initial input, `${DEPS.task.path}`
points at completed dependency results, `${STATE.task_results.task.path}` points
at execution state, and `${TRIGGER.result}` points at the raw TriggerFlow
trigger payload. In DAG task `inputs`, whole-string placeholders preserve the
original runtime value type; embedded placeholders stringify into the
surrounding text.

## Architecture

The DAG capability is split into four stages:

- `AgentlyTaskDAGPlanner` generates deterministic `TaskDAG` data with Agently
  output schema, `ensure_keys`, and validation retry.
- `TaskDAGValidator` validates DAG syntax, dependencies, schema version,
  semantic outputs, side-effect policy, and resolver availability.
- `TaskDAGResolver` maps `task.binding`, `task.id`, then `task.kind` to a
  runnable handler.
- `TaskDAGExecutor` compiles the validated DAG to ordinary TriggerFlow chunks
  and runs it through TriggerFlow lifecycle, stream, pause/resume, result, and
  runtime resource mechanics.

`bindings` is not part of the public facade. Use `handlers` for custom local
functions. Use explicit resource slots such as `planner`, `model`, `actions`,
and `skills` when a task may use them; `actions` and `skills` are not exposed to
the planner unless passed by the caller.

## Resolver Semantics

Custom handlers should use clear names ending in `_handler`:

```python
task = Agently.create_dynamic_task(
    target="review policy",
    plan=task_dag,
    handlers={"risk_check_handler": risk_check_handler},
)
```

In the DAG:

```python
{"id": "check_risk", "kind": "local", "binding": "risk_check_handler"}
```

Unknown optional handlers may be safely pruned by the validator when they do not
affect required semantic outputs, required downstream nodes, approvals, or
side-effect policy. Pruned nodes are recorded in `diagnostics`; unknown required
handlers fail closed before execution.

## Lower-Level Control

Use the low-level classes only when a framework integration needs staged
control:

```python
from agently.builtins.plugins import AgentlyTaskDAGPlanner
from agently.core import TaskDAGResolver, TaskDAGExecutor, TaskDAGValidator

resolver = TaskDAGResolver({"risk_check_handler": risk_check_handler})
validator = TaskDAGValidator(resolver)
planner = AgentlyTaskDAGPlanner(validator=validator)

graph = await planner.async_plan(planner_agent, {"target": "review policy"})
validation = validator.validate(graph, strict_schema_version=True)
snapshot = await TaskDAGExecutor(resolver, validator=validator).async_run(graph)
```

The executor does not depend on Agent. Model and action access belong to the
facade or resolver adapters, while TriggerFlow remains the lower-level
execution substrate rather than the DAG owner API.

## Examples

Use the examples in `examples/dynamic_task/` by layer:

- `01_dynamic_task_basic.py`: submitted `TaskDAG` smoke example with local
  handlers only.
- `02_support_response_module_model.py`: model-powered support module with a
  simple `SupportResponseModule.respond(ticket)` facade, backend fan-out/join
  stages, structured model outputs, mocked business-system lookups, and a
  printed customer-facing response.
- `03_contract_risk_review_business.py`: contract risk review business example
  with a simple `ContractRiskReviewService.review(contract)` facade,
  deterministic local handlers, backend risk scoring, and a printed risk memo.
- `04_incident_briefing_auto_plan.py`: auto-planned incident briefing example
  with a simple `IncidentBriefingService.brief(report)` facade. The model
  creates the `TaskDAG`; the DAG validator and executor run it, while the
  frontstage briefing shape is enforced through Agently `output_schema`.
- `05_enterprise_renewal_complex_auto_plan.py`: complex auto-planned renewal
  example where the model planner creates several independent analysis roots,
  joins them into a synthesis stage, and produces a structured recovery package.
- `06_dynamic_task_config_plan.py`: submitted `TaskDAG` loaded from YAML config
  through `TaskDAG.from_yaml(...)`.
