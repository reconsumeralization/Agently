# Agent Auto-Orchestration

Agently 4.1.3 makes `agent.start()` the default user-layer entrypoint for an
Agent turn. It keeps returning the business result, while the Agent can route
through ordinary model response, Actions, or Skills Executor when those
capabilities were explicitly injected.

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

Candidate injection is the boundary. If no Actions, Skills, or Skills Packs are
registered, `agent.start()` remains an ordinary model request.

`TaskDAG` is the foundation DAG capability. `DynamicTask` remains a
compatibility and convenience facade over DAG planning/execution, not a second
recommended task lifecycle and not an AgentTaskLoop auto-strategy route. Use
TaskDAG / DynamicTask when the application or a visual automation surface owns
the graph shape and wants to run that graph explicitly.

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
Required Skills candidates run through the Skills route. When several optional
candidates are present, such as model-decision Skills and ordinary Actions, the
model chooses the route by default. If there is only one optional candidate,
that route is selected directly.

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
whether an execution is a goal-pursuit task. `budget.iteration_limit`,
`model_call_limit`, and `wall_time_seconds` are soft strategy metadata: they
may shape planning, reflection, repair posture, and evidence depth, but they do
not silently set task-loop `max_iterations` or AgentExecution hard limits. Use
explicit task options or `limits={...}` when the host needs hard resource
controls. By default, AgentTask does not impose model-request, iteration,
TaskBoard tick, or Action round quotas; no-progress and idle timeouts remain
liveness guards for stuck executions, not strategy evidence. Completion still
requires both model verification and host guards.
For task-strategy executions, effort also controls reflection density:
`low` records the final reflection and only planner-marked important process
points, `medium` reflects after each major task node or TaskBoard card/tick, and
`high` reflects after every framework-observable bounded step, Action/ACP call,
TaskBoard card, and final result. Reflection records are Workspace evidence and
verifier/replan input, but they are not completion evidence by themselves.

`execution.step_plan` is retained only as compatibility guidance. Users
normally do not need to spell it out. AgentTaskLoop no longer uses TaskDAG /
DynamicTask as an internal bounded step strategy; legacy `dynamic_task` /
`execution_dag` step proposals and `execution={"step_plan": "dag"}` are
degraded to direct bounded execution with diagnostics. Use TaskDAG / DynamicTask
separately when the host owns a submitted or visual automation graph.

## AgentTask Loop

Use `agent.create_task(...)` when the business goal needs a bounded multi-round
loop instead of one direct AgentExecution. It returns a task-strategy
`AgentExecution` draft; internally the retained `AgentTask` record runs one task
owned by one Agent: plan, execute one bounded step, write Workspace evidence,
verify, replan when needed, then finish as complete or blocked.

Internally, `flat` and `taskboard` are coordination strategies, not separate
execution carriers. Both lower strategy-owned work units through the internal
Block carrier into `ExecutionPlan` / Blocks / TriggerFlow evidence. The
TaskBoard primitive still owns board scheduling, dependency state, and patch
validation; AgentTask uses the carrier for bounded card execution evidence.

In the current 4.1.3 line this is a hardened bounded public task-loop strategy,
not the full future AgentTask system. `agent.create_task_loop(...)` is the explicit spelling
for the same long-task strategy when code wants to make the strategy choice
visible. Both APIs still return `AgentExecution`; new code should
consume data, text, stream, metadata, status, and task refs through
`execution.get_result()` or the execution stream/meta facade instead of treating
`AgentTask` as a second public lifecycle.

`execution="auto"` is the default task execution strategy. In `auto`, AgentTaskLoop
asks the model for a natural-language task-shape analysis plus a thin structured
`execution_hint`, then the strategy policy resolves the actual shape to `flat`
or `taskboard`. The hint is only strategy evidence; TaskBoard does not classify
the task, and the verifier cannot accept the hint as completion evidence. Use
`execution="flat"` or `.strategy("flat")` to force the linear loop, and
`execution="taskboard"` or `.strategy("taskboard")` only when the host
explicitly wants TaskBoard. Nested AgentExecution instances inherit the parent
strategy context unless the child explicitly calls `.strategy(...)`.

```python
agent.language("en")

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
            # Optional compatibility alias for progress narration only.
            # "progress_language": "en",
        },
    },
)

result = execution.get_result()

async for item in result.get_async_generator():
    if (item.meta or {}).get("stream_kind") == "progress_delta":
        print(item.delta or "", end="", flush=True)
    elif (item.meta or {}).get("stream_kind") == "progress":
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
emitted snapshot and task metadata. Model-generated progress is streamed as
`stream_kind="progress_delta"` delta events while the sentence is being written,
then emitted once as a complete `stream_kind="progress"` item for stable logs
and UI state. Set `options={"agent_task": {"progress_language": "zh-CN"}}` to
control progress narration for one execution, or set `agent.language("zh-CN")`
as the preferred Agent-level policy for final output, process text, progress
text, and Search/Browse locale hints. `execution.language("zh-CN")` applies the
same policy to one AgentExecution draft. `progress_language` and
`agent_task.progress.language` remain compatibility controls for progress text
only; `auto` keeps the framework default. The main loop does not produce extra fields for progress
narration and does not wait for progress narration to finish.
Progress narrator failures are side-channel diagnostics and warning-level
runtime events; they do not turn the main execution into `model.request_failed`.
Model progress narration receives an operator-safe snapshot; developer
diagnostics such as low-level Workspace/SQLite fallback details remain in
snapshots and `task.meta()["diagnostics"]`, but are omitted from the progress
model input.

For text consumers, `get_async_generator(type="delta")` remains the public text
stream. In task-strategy executions it includes model-generated text increments
and also projects selected process events into paragraph text: template progress,
snapshots, heartbeat status, phase status, retry markers, and the terminal task result.
Use `type="instant"` when the UI needs the original structured event payloads
with `path`, `value`, `delta`, `is_complete`, and `meta`.

During long quiet waits, AgentTask may emit an `agent_task.heartbeat` stream item
after `agent_task.heartbeat_interval_seconds` seconds without any other stream
item. The default interval is 10 seconds. Heartbeats are observational status
only: they help UI and log consumers understand the current stage, but they do
not satisfy evidence, hide a stall, or replace request/no-progress and
task-deadline timeouts. Any normal progress, snapshot, child-execution, delta,
or phase event resets the quiet timer, so active streams do not get heartbeat
spam.

Terminal task status and artifact acceptance are separate. `completed` means
the verifier accepted the result (`accepted=True`, `artifact_status="accepted"`).
`max_iterations` can still leave a useful Workspace file or checkpoint, but it
is a partial artifact (`accepted=False`, `artifact_status="partial"`), not a
completed business result.

When a bounded step or TaskBoard card returns a short `artifact_markdown` body
or a sectioned `artifact_manifest`, AgentTask writes the deliverable through the
bound Workspace and immediately reads it back. The cold evidence records
`path`, `bytes`, `sha256`, bounded preview, and `file_refs`; model-hot verifier
input uses path/ref handles, bounded content or preview, and truncation status.
For long, sectioned, or prose-heavy
deliverables, choose the content carrier deliberately. A single freeform
document can draft as natural Markdown/plain text with no `.output()` contract.
When the caller needs separately addressable fields, use Agently
`.output(..., format=...)` with `xml_field`, `hybrid`, or `yaml_literal` when
that format fits the payload instead of forcing the long body into compact JSON
fields. Keep status, evidence, and verification in separate compact
judgment/readback contracts. When AgentTask must deliver a trusted file
artifact, use `artifact_manifest.sections` plus Workspace readback.
Model-declared `file_refs` are diagnostics only until the framework has
produced this Workspace readback evidence, preserving a real `final.md` or other
deliverable for host-side review.

The same ref-backed path is also valid for intermediate work. A step may
download a file, save a webpage snapshot, write generated code, keep search
notes or memory-like task notes, or persist large extracted text as Workspace or Action artifact refs.
Hot prompts should carry compact refs and bounded previews; later blocks can
open scoped snippets with `read_file(max_bytes=..., offset=...)` or artifact
readback when they need the content. Readback work-unit hot payloads use the
same compact refs; complete refs remain in cold Workspace/Blocks evidence for
programmatic readback and audit. These intermediate refs are execution evidence,
not final deliverable proof. A discovered URL, path, download, or
snapshot ref is also not evidence that the content has been read; it remains
`ref_only` until a bounded readback or content preview is visible.
Source-grounded deliverables should either request structured `target_refs`
readback for unread refs or label them as discovered-only rather than claiming
facts from them. If an Action artifact readback exposes Workspace `file_refs`
for a materialized download, TaskBoard readback promotes those nested refs to
card-level `file_refs` so later work can use Workspace readback instead of
relying on a buried JSON preview. When a non-final
TaskBoard card proposes a required final path such as `final.md`, AgentTask
relocates that intermediate artifact to `working/taskboard/<card-id>/...` and
keeps the declared final path for the final synthesis or finalization card.
Framework-generated final repair or continuation cards that are marked with the
required final deliverable path are authorized to write that path, so repair
does not loop by repeatedly producing only working evidence files.
Flat source refs carry the same boundary: repository clone/list manifest paths
are `ref_only` until a file read, artifact readback, or bounded content preview
is visible. A verifier or repair planner can reuse exact paths as retrieval
targets, but not as proof of file contents.

Flat and TaskBoard work units also receive a task context contract with
compact `current_time` facts: `utc`, plus `local` and `timezone` when the local
timezone is recognizable. For current, latest, recent, or as-of
tasks, use that time context unless the caller supplied a more specific date.
The contract is context for model decisions, planning, evidence selection, and
source-boundary handling; it does not set model-call, tool-call, node-count,
iteration, or wall-clock caps.

TaskBoard readback cards can inspect both Action artifact refs and trusted
Workspace file refs with bounded cold readback previews. Framework-generated
readback cards scope evidence to direct dependencies plus upstream evidence
cards, so a control-card readback can still inspect Action refs produced by
earlier evidence-gathering cards. If a generated continuation card still
reports that the same readback is insufficient, the framework does not
recursively synthesize another readback/continuation chain; the card must
propose different executable work or remain blocked with diagnostics.
For scoped Workspace retrieval, `evidence_snippet` records include whether the
bounded snippet was `truncated`. If a TaskBoard card with scoped retrieval
returns blocked/insufficient output without an explicit next action, AgentTask
turns that local insufficiency into an action-capable evidence card with an
expanded bounded retrieval plan plus a continuation card. The search result is
still only factual context; the continuation card decides whether it is enough.
When the missing evidence is a new concrete URL, path, or ref rather than an
existing Action/Workspace ref, the control card should return
`next_board_action="readback"` plus structured `target_refs`. AgentTask turns
that compact intent into an action-capable evidence card that can download,
snapshot, or otherwise materialize the target before the continuation card
runs. URLs mentioned only inside `gaps` prose are diagnostics; they are not
parsed as executable targets.
When a control card instead returns `next_board_action="patch"` with a Workspace
text patch proposal, AgentTask applies the patch to the bound Workspace file,
writes it back, and returns trusted `file_refs` after readback. This is a
materialization step only: final completion still belongs to terminal
acceptance and host guards.
For completed and sufficient control outputs, non-fatal `gaps` do not prevent
Workspace artifact materialization; `remaining_work`, blocked status, repair,
or readback still do. Writing the artifact only creates evidence for later
readback and verification. It does not mean the final task has been accepted.
Flat and TaskBoard do not need an independent verifier after every intermediate
work unit. In Flat, non-empty `remaining_work` defaults the current step to an
intermediate result, so the next iteration consumes the new facts and decides
the next action; a step may also return `ready_for_final_verification=false` to
make that explicit. Set `ready_for_final_verification=true` only when the
current result intentionally needs terminal, blocking, or risk verification
now. In TaskBoard, the downstream card that consumes
dependency evidence decides whether it is enough for its own objective.
Independent verifier requests are for final acceptance, fan-in/control
acceptance, evidence/artifact boundary audit, contradictions, or high-risk
review.
When a terminal verifier returns an incomplete result, its compact
`repair_context` is carried into the next Flat work unit and into the dedicated
Workspace artifact draft request when the next deliverable body is file-backed.
This keeps exact `acceptance_delta`, repair constraints, next-step
requirements, and available evidence anchors visible to the consumer that
actually rewrites or reads the artifact, without putting cold integrity metadata
back into the model-hot path.

AgentTask observation also publishes normalized action facts on the structured
stream as `agent_task.action.started`, `agent_task.action.completed`, and
`agent_task.action.failed`. These events summarize existing Action records with
safe input summaries, result previews, refs, timing, diagnostics, and work-unit
ownership. Recovered `success` or `partial_success` Action records are projected
as completed observations, while failed events are reserved for actual failed,
blocked, timed-out, or unrecovered error records. They are observation facts for DevTools, UI, and experiment logs; the
downstream consumer, terminal verifier/final control, and strategy still own
usefulness, quality, and completion judgment.

When the write succeeds and readback is trusted, verifier input includes the
model-hot readback content/preview, compact refs without checksum fields, and
`capability_evidence.artifacts.readback` path handles; with
`max_iterations=1`, a real readable artifact should not become partial only
because the evidence chain was omitted. If readback fails or lacks trusted
`path` / `bytes` / `sha256` evidence, diagnostics use
`agent_task.workspace_artifact.readback_failed` or
`agent_task.workspace_artifact.readback_insufficient` so the problem is reported
as Workspace artifact readback missing/insufficient, not as a generic budget or
iteration shortfall.

If the structured task input or output contract declares required deliverables,
AgentTask host guards require those Workspace files to exist and read back before
accepting completion. A verifier response that says a file exists is not enough
unless Workspace readback confirms the declared final path.

For public reference material, such as framework introductions or API guidance,
task verifier acceptance is still not a source-quality guarantee by itself. Feed
current docs/spec/source references into the task or add an Agently model-judge /
source-reference check so stale or generalized API claims cannot pass only
because the task-level verifier accepted the draft.

Intermediate process steps with strong structured contracts use Agently
`.output(..., format=...)` on the owning `ModelRequest` or `AgentExecution`.
Do not add a restrictive JSON `.output()` contract to a pure long-prose drafting
request only to control the body. Compact control payloads can use JSON; when a
structured contract is genuinely needed for a content-heavy payload, use
`xml_field` for XML-like field boundaries, `hybrid` for prose plus typed control
fields, or `yaml_literal` for a literal document payload when that format fits
the target model and consumer. If a declared non-JSON format cannot be parsed,
Agently tries JSON as a recovery parser and accepts it only when the parsed
value is a dict. The same guard applies to task `final_result` when an
execution output contract is present.

`examples/agent_task/goal_effort_public_stream.py` is the public-chain
streaming proof for this contract. It runs
`.goal(...).effort(...).input(...).output(...).strategy("flat")`, consumes
`get_async_generator()`, streams model-generated progress deltas, and checks
that the execution prompt snapshot reaches AgentTaskLoop planning, execution,
and verification. `examples/agent_task/goal_pursuit_acceptance_matrix.py`
remains a smaller matrix script for accepted and non-accepted terminal states.

`examples/agent_task/real_complex_bundle_goal_stream.py` is the high-level real
complex proof for the same path. It mounts Search, AMap MCP, Workspace file
actions, and the CocoonAI `architecture-diagram` Skill through public Agent
capability APIs, then asks the task loop to produce an operator daily report, a
Hangzhou business travelogue, and an HTML/SVG architecture diagram while
streaming natural-language progress deltas. It uses multi-round bounded direct
steps so the proof stays on the current public AgentTask lifecycle instead of
depending on mixed DynamicTask/DAG execution. The lower-level
`examples/blocks/07_real_complex_bundle_stream.py` remains a Blocks
external-capability substrate probe rather than the recommended business entry
point.

`examples/agent_task/agently_architecture_diagram_task.py` is the longer
design-document experiment for the same path. It uses
`.goal(...).effort(...).strategy("task")` as the compatibility spelling for an
AgentTaskLoop draft, a repository-source Action, Workspace file Actions, and an
independent Agently model judge to produce and review a readable Agently
architecture diagram. The execution shape is still resolved by the task
strategy layer unless the host explicitly selects `flat` or `taskboard`.

The first public slice is intentionally narrow: single task, one Agent owner,
roughly 2-5 iterations, and bounded steps through `AgentExecution`. Those steps
may use Actions or Skills that the host already enabled on the Agent or attached
to the current execution. AgentTask does not provide
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
execution.input("Summarize the reviewed DAG snapshot for the operator.")
execution.info({"dag_snapshot": snapshot})

async for item in execution.get_async_generator(type="instant"):
    if item.is_complete:
        print(item.path, item.value)

data = await execution.async_get_data()
meta = await execution.async_get_meta()
```

The execution object follows the same consumption style as model responses:
`get_data`, `get_text`, `get_meta`, `get_generator`, and async equivalents.
The default stream is `type="delta"` and yields plain text strings, including
the reserved `"<$retry>{reason}</$retry>"` boundary when a model stream is
replayed. Use `type="instant"` for structured execution events:
`AgentExecutionStreamData` keeps the familiar `path`, `value`, `delta`, and
`is_complete` fields and adds route metadata for process-level events.

`create_execution()` creates an AgentExecution draft. Ordinary prompt-only
drafts run as direct model requests. DynamicTask/TaskDAG workflows run through
`Agently.create_dynamic_task(...)` or `TaskDAGExecutor(...)` first and can pass
their snapshots into a later AgentExecution as evidence. When a developer-owned
loop or task strategy needs a bounded AgentExecution step, express the boundary
with `lineage` and `limits`:

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
across direct model routes and Skills model stages.
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
progress from route selection, model streaming, Skills, or ActionRuntime
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

Submitted DAGs routed through the independent DynamicTask facade keep using DAG
runtime placeholders such as `${INIT.ticket}` and `${DEPS.lookup}` inside task
`inputs`. The graph input source is resolved in this order:

```text
async_run(graph_input=...)
> {"target": task_target}
```

This keeps DAG input explicit and separate from AgentExecution prompt routing:

```python
task = Agently.create_dynamic_task(
    target="review ticket",
    plan=graph,
    handlers=handlers,
)
snapshot = await task.async_run(graph_input={"ticket": "TICKET-OK"})
```

If an AgentExecution needs the result, pass the snapshot as ordinary evidence in
`input(...)`, `info(...)`, or a Workspace record. The DAG snapshot does not by
itself mean the broader business goal is complete.

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

Executor routes bridge route and ModelRequest instant checkpoints so services
can stream route decisions, task/action progress, selected model field deltas,
and final semantic outputs.
If a TriggerFlow-backed route fails, the Agent execution stream is closed and
the original error is raised to the consumer instead of leaving
`get_async_generator(...)` waiting for more items.

For independent TaskDAG model nodes, consume the TriggerFlow runtime stream and
normalize `task_dag.model_field` items if the host wants field-level display:

```python
task = Agently.create_dynamic_task(target="reply", plan=graph, handlers=handlers)
execution = task.compile(graph).create_execution(auto_close=False)
async for item in execution.get_async_runtime_stream({"ticket": ticket}, timeout=None):
    if item.get("type") == "task_dag.model_field" and item.get("field_path") == "reply":
        print(item.get("delta") or "", end="", flush=True)
```

This keeps AgentExecution stream semantics separate from independent DAG
runtime stream semantics.
