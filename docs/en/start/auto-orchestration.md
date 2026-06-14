# Agent Auto-Orchestration

Agently 4.1.3 makes `agent.start()` the default user-layer entrypoint for an
Agent turn. It keeps returning the business result, while the Agent can route
through ordinary model response, Actions, Skills Executor, or a DAG-shaped
execution route when those candidates were explicitly injected.

```python
result = (
    agent
    .use_actions([market_data_action])
    .use_skills_packs(["equity-research"])
    .input("Review this renewal risk.")
    .output({"answer": (str, "final answer", True)})
    .start()
)
```

Candidate injection is the boundary. If no Actions, Skills, Skills Packs, or
DAG candidates are registered, `agent.start()` remains an ordinary model
request.

`TaskDAG` is the foundation DAG capability. `DynamicTask` remains a
compatibility and convenience facade over DAG planning/execution, not a second
recommended task lifecycle. `agent.use_dynamic_task(...)` registers an
Agent-level DAG candidate for later executions. `execution.use_dynamic_task(...)`
registers a candidate only on the captured `AgentExecution` draft, so one DAG
route does not leak into unrelated Agent runs.

Quick prompt chains create execution-scoped drafts. The Agent can be kept as a
service singleton for shared settings, model activation, Actions, Skills,
Workspace, and `define(...)` / `always=True` prompt, while `.input(...)`,
`.system(...)`, `.output(...)`, attachments, and per-execution options in one
chain are written to an isolated `AgentExecution` draft:

```python
results = await asyncio.gather(
    agent.input("Summarize request A").async_start(),
    agent.input("Summarize request B").async_start(),
    agent.input("Summarize request C").async_start(),
)
```

For multi-statement setup, capture the execution draft explicitly:

```python
execution = agent.create_execution()
execution.input("Review this renewal risk.")
execution.output({"answer": (str, "final answer", True)})
result = await execution.async_start()
```

Do not rely on `agent.input(...); agent.output(...); await agent.async_start()`
for execution prompt accumulation. Use `always=True`,
`set_agent_prompt(...)`, or stable setup methods for Agent-lifetime state; use
`agent.define(...)` for reusable Agent definition state; use
`agent.create_request(...)` / `agent.request` only when you intentionally want
the lower-level request-builder surface.

Accepted development-line routing is candidate-driven and deterministic-first.
Submitted DAG candidates through the DynamicTask facade take precedence and
required Skills candidates run through the Skills route. When several optional
candidates are present, such as DAG-shaped execution, model-decision Skills,
and ordinary Actions, the model chooses the route by default. If there is only
one optional candidate, that route is selected directly.

The public Agent API stays in core, but route planning and execution are owned
by the active `AgentOrchestrator` plugin through the `AgentOrchestrator`
protocol. This keeps Skills, the DAG substrate, and future route
implementations replaceable without teaching core about builtin plugin
internals.

## Goal Pursuit

Use `agent.goal(goal_or_goals, success_criteria=None)` when the business goal
needs a bounded plan, execution, evidence, verification, and replan loop.
`agent.goals(...)` is only a plural alias for the same entrypoint.

```python
result = (
    agent
    .use_skills("website-builder", "seo-reviewer")
    .use_actions(write_file, read_file)
    .require_actions("write_file")
    .goals(
        [
            "Build a small product website.",
            "Prepare a launch checklist.",
        ],
        success_criteria=[
            "The final artifact is a runnable page file.",
            "The page content covers every supplied business fact.",
            "Execution evidence includes file write, readback, and content inspection.",
        ],
    )
    .effort(
        "high",
        budget={
            "iteration_limit": 4,
            "model_call_limit": 10,
            "wall_time_seconds": 300,
        },
        planning={"depth": "expanded", "max_plan_items": 8},
        verification={"strictness": "strict"},
        replan={"policy": "on_verification_failure", "limit": 2},
        progress={"detail": "phase"},
    )
    .start()
)
```

Simple code can keep using `.effort("low" | "medium" | "high")`. The expanded
form keeps the same owner: effort controls strategy and resource intensity, not
whether an execution is a goal-pursuit task. `budget.iteration_limit` maps to
the task-loop iteration budget, while `model_call_limit` and
`wall_time_seconds` map to AgentExecution limits unless explicit limits were
already set. Completion still requires both model verification and host guards.

`execution.step_plan` defaults to `auto`. Users normally do not need to spell it
out. It lets a Goal Pursuit iteration use DAG as an internal bounded-step shape
when the next unit of work naturally has serial or parallel substeps. Use
`execution={"step_plan": "direct"}` only to force one bounded AgentExecution
step, or `execution={"step_plan": "dag", "max_tasks": 6}` when the caller wants
to prefer a DAG-shaped step and bound its size. The DAG result is folded back
into AgentTaskLoop evidence; it is not accepted task completion until the model
verifier and host guards both pass.

## AgentTask Loop

Use `agent.create_task(...)` when the business goal needs a bounded multi-round
loop instead of one direct AgentExecution. It returns a task-strategy
`AgentExecution` draft; internally the retained `AgentTask` record runs one task
owned by one Agent: plan, execute one bounded step, write Workspace evidence,
verify, replan when needed, then finish as complete or blocked.

In 4.1.3.7 this is a hardened bounded public task-loop strategy, not the full
future AgentTask system. `agent.create_task_loop(...)` is the explicit spelling
for the same long-task strategy when code wants to make the strategy choice
visible. Both APIs still return `AgentExecution`; new code should
consume data, text, stream, metadata, status, and task refs through
`execution.get_result()` or the execution stream/meta facade instead of treating
`AgentTask` as a second public lifecycle.

```python
execution = agent.create_task(
    goal="Upgrade a legacy Agently script so it runs on the current 4.1.x API.",
    success_criteria=[
        "The original failure is recorded.",
        "The script no longer uses incompatible legacy API calls.",
        "The fixed script runs and produces the expected structured result.",
    ],
    workspace="./.agently/tasks/legacy-script-upgrade",
    max_iterations=4,
    verify="before_done",
    options={
        "agent_task": {
            "stream_progress": True,
            "stream_progress_background": True,
            "stream_snapshots": True,
            # Optional: use a separate model key to narrate progress from snapshots.
            # Omit this key to use template progress with no model requests.
            # "progress_model_key": "cheap-progress-model",
        },
    },
)

result = execution.get_result()

async for item in result.get_async_generator():
    if (item.meta or {}).get("stream_kind") == "progress":
        print("[PROGRESS]", item.value["message"])
    elif (item.meta or {}).get("stream_kind") == "snapshot":
        print("[SNAPSHOT]", item.path, item.value["snapshot"])

data = await result.async_get_data()
meta = await result.async_get_meta()
task_refs = result.task_refs
```

Each iteration writes planning decisions, execution observations, verification
evidence, evidence links, and checkpoints to Workspace. Checkpoints use the
Workspace checkpoint-store port, and task evidence relationships use
`workspace.link_evidence(...)`. The next iteration receives a ContextPackage from
`workspace.build_context(...)`, so the loop can carry evidence forward without
turning Workspace into an autonomous planner.

AgentTask verification remains model-owned, but completion acceptance is
conservative. The loop normalizes verifier output and will not accept a task as
complete when missing criteria remain, required action evidence failed or was
blocked, approval is still required, or a required final deliverable is absent.
Those guard decisions are recorded in task diagnostics so the next iteration can
replan from concrete evidence instead of accepting a weak completion claim.

The task-strategy AgentExecution stream emits structured result events and, by
default, compact intermediate `snapshot` items. Natural-language `progress`
items are opt-in with
`options={"agent_task": {"stream_progress": True}}`; the built-in descriptions
are template-based when no `progress_model_key` is configured, so they do not
add model requests or token usage. When `progress_model_key` is set, AgentTask
uses that separate model key in a background task to summarize the already
emitted snapshot and task metadata. The main loop does not produce extra fields
for progress narration and does not wait for progress narration to finish.
Progress narrator failures are side-channel diagnostics and warning-level
runtime events; they do not turn the main execution into `model.request_failed`.
Model progress narration receives an operator-safe snapshot; developer
diagnostics such as low-level Workspace/SQLite fallback details remain in
snapshots and `task.meta()["diagnostics"]`, but are omitted from the progress
model input.

Terminal task status and artifact acceptance are separate. `completed` means
the verifier accepted the result (`accepted=True`, `artifact_status="accepted"`).
`max_iterations` can still leave a useful Workspace file or checkpoint, but it
is a partial artifact (`accepted=False`, `artifact_status="partial"`), not a
completed business result.

`examples/agent_task/goal_pursuit_acceptance_matrix.py` is the lightweight
real-model smoke for this contract. It runs one accepted Goal Pursuit case and
one non-accepted evidence-guard case with model-owned planning and verification,
then prints the verifier and host-guard terminal evidence. Force
`AGENT_TASK_MODEL_PROVIDER=ollama` to reproduce the documented
`max_iterations` / partial output; stricter providers may classify the same
missing Action evidence as `blocked`.

`examples/agent_task/agently_architecture_diagram_task.py` is the longer
design-document experiment for the same path. It uses
`.goal(...).effort(...).strategy("task")`, a repository-source Action,
Workspace file Actions, and an independent Agently model judge to produce and
review a readable Agently architecture diagram.

The first public slice is intentionally narrow: single task, one Agent owner,
roughly 2-5 iterations, and bounded steps through `AgentExecution`. Those steps
may use Actions, Skills, or DAG candidates that the host already enabled on the
Agent or attached to the current execution. AgentTask does not provide
multi-task coordination, background autonomy, distributed leases, mid-step
pause/resume, or long-term memory management. Crash recovery for this slice is
exposed through `agent.resume(...)` / `agent.async_resume(...)`, which rebuild a
task-strategy `AgentExecution` instead of exposing AgentTask as a second public
lifecycle.

### Resume a task after a crash

AgentTaskLoop persists a resumable snapshot after every completed iteration. If
the process crashes, resume the task as a fresh `AgentExecution` and continue
from the next iteration. Completed iterations are not re-executed:

```python
execution = await agent.async_resume("issue-123")   # or agent.resume("issue-123")
result = await execution.async_start()              # continues from iteration N+1
meta = await execution.async_get_meta()
```

Resume reads the task's latest snapshot from the Workspace, restores its
iteration history and cumulative required-capability progress, and raises
`ValueError` if no resumable snapshot exists. An iteration that was in flight at
crash time is re-planned, so non-replay-safe step side effects are the host's
responsibility. `AgentExecutionResult.resume()` delegates to the same Agent
resume facade when the result carries resumable `task_refs`; otherwise it
returns an unsupported resume response. `resume_task(...)` remains only as a
compatibility alias for `resume(...)`.

For examples that validate model-owned semantic content, combine deterministic
smoke checks with a second Agently model-judge request. Structural checks such
as file existence, question count, and visible source labels are useful smoke
gates, but semantic acceptance should come from a judge schema with per-rule
evidence and boolean results.

Business-system fixtures may be mocked in examples, but they should only return
business facts, records, policies, or intentionally incomplete source data.
They should not return pass/fail labels, hidden expected answers, or local
quality verdicts. If the scenario needs to decide whether an artifact handled
defective or conflicting data correctly, let AgentTask verification or a
separate Agently model-judge request make that judgment from explicit rules and
evidence.

## Execution Object

Use `agent.create_execution()` when the caller needs route diagnostics, multiple
result views, or process streaming:

```python
execution = agent.create_execution()
execution.input("Run the reviewed graph.")
execution.use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)

async for item in execution.get_async_generator(type="instant"):
    if item.is_complete:
        print(item.path, item.value)

data = await execution.async_get_data()
meta = await execution.async_get_meta()
```

The execution object follows the same consumption style as model responses:
`get_data`, `get_text`, `get_meta`, `get_generator`, and async equivalents.
Execution streams yield `AgentExecutionStreamData` from `agently.types.data`.
This type keeps the familiar `path`, `value`, `delta`, and `is_complete` fields
and adds route metadata for process-level events.

`create_execution()` creates an AgentExecution draft. Ordinary prompt-only
drafts run as direct model requests. When a developer-owned loop or task
strategy needs a bounded step, express the boundary with `lineage` and
`limits`:

```python
execution = agent.input("Try one bounded fix step.").create_execution(
    lineage={
        "task_id": "issue-123",
        "iteration_id": "iter-2",
        "step_id": "execute-fix",
        "parent_execution_id": "exec-prev",
    },
    limits={
        "max_model_requests": 3,
        "max_seconds": 180,
        "max_no_progress_seconds": 60,
    },
)
```

This is still one AgentExecution, not a multi-turn loop. `lineage` provides
stable correlation, while `limits` provides shared model-request budget counting
across direct model routes, TaskDAG model tasks, and Skills model stages.
Use `None` for an unlimited budget.

If a bounded execution exceeds its model-request budget, Agently raises
`AgentExecutionLimitExceeded`, available from the root `agently.core` export or
from `agently.core.application.AgentExecution`. The execution meta remains
inspectable and records `status="blocked"` plus the limit event in
`diagnostics`.

For stuck executions, `limits.max_seconds` is a hard deadline for the whole
AgentExecution. In Goal Pursuit / task-strategy runs, AgentTaskLoop owns that
wall-clock budget and returns a task `timed_out` result with task metadata; other
routes surface the hard deadline as `RuntimeStageStallError`, available from the
root `agently.core` export or from `agently.core.application.AgentExecution`.
`limits.max_no_progress_seconds` is an idle stall boundary: any accepted runtime
progress from route selection, model streaming, TaskDAG, Skills, or ActionRuntime
refreshes the timer. `async_get_meta()` remains inspectable and records
`status="timed_out"` or `status="stalled"` with `diagnostics["timeouts"]` /
`diagnostics["stalls"]` and the last progress event.

Provider and response materialization waits have separate knobs:

```python
Agently.set_settings("OpenAICompatible.stream_idle_timeout", 60.0)
Agently.set_settings("OpenAIResponsesCompatible.stream_idle_timeout", 60.0)
Agently.set_settings("response.materialization_idle_timeout", 60.0)
```

`stream_idle_timeout` bounds the gap after the first provider stream event.
First-event timeout and stream-idle timeout both raise
`RuntimeStageStallError` with provider/model fields when the requester can
identify them.
`response.materialization_idle_timeout` bounds the wait while final text, data,
object, or meta is materialized from the response parser. `None` is unlimited;
`-1` is accepted for compatibility. If the provider or response construction
emits an explicit stream error before materialization completes,
`get_text()` / `get_data()` / `get_meta()` propagates that original error
instead of waiting for the materialization timeout.

High-frequency RuntimeEvent outlets should request Event Center summary
delivery instead of asking AgentExecution to throttle at the source:

```python
Agently.event_center.register_hook(
    handler,
    event_types="model.response.delta",
    hook_name="app.delta_summary",
    delivery_policy={"mode": "summary", "emit_interval": 0.1, "max_items": 20},
)
```

AgentExecution stream APIs stay raw. Event Center outlet summaries include
`meta["coalesced"]`, `coalesced_count`, and source event ids when a hook opts in
to summary delivery.

`async_get_meta()` includes `lineage`, `limits`,
`route`, `route_plan`, `logs`, `diagnostics`, and `workspace_refs`. `logs` is
the route-independent place to inspect runtime
facts such as model response ids, ActionRuntime action records, and artifact
refs:

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
item.meta["lineage"]["task_id"]
```

Default Agents carry a lazy Workspace binding, and `agent.use_workspace(...)`
can override it with an explicit root or provider before `create_execution()`.
AgentExecution still does not decide what becomes memory automatically; persist
explicitly from the execution side:

```python
workspace_record = await execution.async_record_workspace(
    collection="observations",
    kind="agent_execution_observation",
    content={"result": data},
    checkpoint=True,
)
```

This writes through the execution's bound Workspace provider surface. When a
checkpoint is requested, the helper uses the checkpoint-store port and records
an evidence link between the AgentExecution record and the checkpoint. The
record id, checkpoint id, and evidence link id are visible under
`meta["workspace_refs"]`. Workspace remains the durable substrate and does not
need to know AgentExecution strategy semantics. Call `workspace.build_context(...)`
for the next step.

For development diagnostics, attach an EventCenter observation hook or
temporarily enable console details:

```python
Agently.event_center.register_hook(print, event_types=None, hook_name="debug")
agent.set_settings("debug", "detail")
```

Use this only while debugging route selection, model requests, ActionRuntime, or
Workspace persistence. Remove debug hooks/settings from examples and production
snippets once the problem is understood.

## Submitted DAG Input

Submitted DAGs routed through the DynamicTask facade keep using DAG runtime
placeholders such as `${INIT.ticket}` and `${DEPS.lookup}` inside task
`inputs`. Under an Agent route, the graph input source is resolved in this
order:

```text
use_dynamic_task(graph_input=...)
> the execution prompt snapshot input slot
> {"target": task_target}
```

This lets ordinary Agent prompt code feed a submitted DAG without inventing a
second mapping surface:

```python
execution = agent.create_execution()
execution.input({"ticket": "TICKET-OK"})
execution.use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)
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

For TaskDAG model nodes, structured output fields stream under stable paths:

```python
async for item in execution.get_async_generator(type="instant"):
    if item.path == "task_dag.tasks.reply.fields.reply" and item.delta:
        print(item.delta, end="", flush=True)
```

This preserves model-response `instant` semantics while keeping process-stream
paths owned by the Agent execution route.
