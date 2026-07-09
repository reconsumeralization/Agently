---
title: Agently 4.1.4 Release Notes
description: Agently 4.1.4 从 4.1.3 到最终状态的升级说明，覆盖 AgentExecution、AgentTask、TaskBoard、Workspace、TriggerFlow、Skills、ActionRuntime、model runtime、observability 与 typing。
keywords: Agently, release notes, 4.1.4, AgentExecution, AgentTask, TaskBoard, Workspace, TriggerFlow, SkillsExecutor, ActionRuntime
---

# Agently 4.1.4 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.4.md) · **中文**

Agently 4.1.4 围绕执行所有权、长任务交付、durable context、runtime orchestration、capability control、可观测 model/action execution 完成升级。

## 核心结果

Agently 4.1.4 将 `AgentExecution` 收敛为稳定的公开 run surface，并把长任务执行、Workspace evidence、ActionRuntime capabilities、TriggerFlow orchestration 和 runtime observation 放进同一套使用形态：

```text
业务输入
  -> AgentExecution
  -> direct / flat / taskboard strategy
  -> Actions / Skills / Workspace / TaskDAG / TriggerFlow
  -> EvidenceEnvelope + Workspace readback
  -> verifier + host guards
  -> final_response + structured result + RuntimeEvents
```

## 关键样例代码

### Direct AgentExecution

```python
result = (
    agent
    .input("Summarize the renewal risk and recommend the next action.")
    .output({
        "summary": (str, "short business summary", True),
        "risk_level": (str, "low / medium / high", True),
        "next_action": (str, "recommended next action", True),
    })
    .strategy("direct")
    .get_result()
)

data = result.get_data()
text = result.get_text()
meta = result.get_meta()
```

### Task Strategy With Workspace Evidence

```python
result = (
    agent
    .use_workspace("./.agently/tasks/migration-risk")
    .goal(
        "Prepare a migration risk report.",
        success_criteria=[
            "Cover compatibility, rollout, and rollback risks.",
            "Ground each recommendation in available evidence.",
            "Produce a final artifact that can be read back from Workspace.",
        ],
    )
    .effort("medium")
    .strategy("auto")
    .output({
        "executive_summary": (str, "final summary", True),
        "top_risks": ([str], "material migration risks", True),
        "recommended_plan": (str, "recommended rollout plan", True),
    })
    .get_result()
)

final_text = result.get_text()
task_payload = result.get_data()
task_meta = result.get_meta()
```

### Explicit TaskBoard Delivery

```python
execution = agent.create_task(
    goal="Complete the vendor security questionnaire.",
    success_criteria=[
        "Every required question has an answer.",
        "Each answer is grounded in supplied policy evidence.",
        "The final Markdown file is written and read back from Workspace.",
    ],
    execution="taskboard",
    workspace="./.agently/tasks/security-questionnaire",
)

execution.output({
    "final_file": (str, "Workspace path for the final Markdown file", True),
    "summary": (str, "short completion summary", True),
})

result = execution.get_result()

async for item in result.get_async_generator(type="instant"):
    render_status(item.path, item.value)

answer = await result.async_get_text()
data = await result.async_get_data()
```

### Runtime Guidance During A Task

```python
import asyncio

execution = agent.create_task(
    goal="Prepare the incident handoff.",
    success_criteria=["The handoff reflects the latest operator context."],
    execution="flat",
    workspace="./.agently/tasks/incident-handoff",
)

run_task = asyncio.create_task(execution.async_get_data())

await execution.async_add_guidance(
    "Use the newly uploaded incident note as the primary source.",
    author="operator",
)

data = await run_task
meta = await execution.async_get_meta()
guidance_refs = meta["task_refs"]["workspace_refs"]["guidance"]
```

### Workspace Records And Retrieval

```python
workspace = Agently.create_workspace("./.agently/support-memory")

await workspace.put(
    collection="memory",
    kind="project_note",
    content="Customer prefers staged rollout with rollback checkpoints.",
    tags=["customer", "rollout"],
    source={"type": "operator_note"},
)

context = await workspace.retrieve(
    query="What rollout constraints should the migration report remember?",
    tags=["customer", "rollout"],
    sources=["records", "files"],
    budget={"chars": 12000},
    selection="length",
)

exact_hits = await workspace.grep(
    "rollback",
    filters={"collection": "memory", "kind": "project_note"},
)
```

### Session Memory

```python
from agently.core import Session

workspace = Agently.create_workspace("./.agently/support-memory")

session = Session()
session.use_memory(mode="AgentlyMemory", workspace=workspace)

agent = Agently.create_agent("support-agent").use_workspace(workspace)
agent.activate_session(session_id="support-demo")
agent.activated_session.use_memory(mode="AgentlyMemory")
```

### TriggerFlow Execution

```python
from agently import TriggerFlow, TriggerFlowRuntimeData

flow = TriggerFlow(name="approval-backed-workflow")

async def prepare(data: TriggerFlowRuntimeData):
    await data.async_set_state("ticket_id", data.input["ticket_id"])
    return {"ticket_id": data.input["ticket_id"], "amount": data.input["amount"]}

async def finish(data: TriggerFlowRuntimeData):
    decision = data.input if isinstance(data.input, dict) else {}
    await data.async_set_state("approved", bool(decision.get("approved")))

flow.to(prepare).to(finish)

execution = flow.create_execution(auto_close=False)
await execution.async_start({"ticket_id": "T-100", "amount": 1200})
state = await execution.async_close()
```

### Skills With AgentExecution

```python
result = (
    agent
    .use_workspace("./.agently/tasks/release-readiness")
    .use_skills("release-readiness-reviewer")
    .goal(
        "Review release readiness and produce a go/no-go recommendation.",
        success_criteria=[
            "Check validation evidence.",
            "Identify blocking risks.",
            "Return a structured release decision.",
        ],
    )
    .effort("medium")
    .output({
        "decision": (str, "go / no-go", True),
        "blocking_risks": ([str], "release blocking risks", True),
        "followups": ([str], "required follow-up actions", True),
    })
    .get_result()
)
```

## 最终推荐用法

| 场景 | 最终推荐用法 | 主要 API / 表面 |
|---|---|---|
| 普通一次性 Agent run | 保持 direct run，并消费 `AgentExecutionResult`。 | `agent.input(...).output(...).get_result()`；`result.get_data()`；`result.get_text()` |
| 多语句 run 配置 | 创建或持有一个 execution draft，再把 prompt、output、actions、Skills、Workspace、strategy 绑定到同一个 draft。 | `execution = agent.create_execution()`；`execution.input(...)`；`execution.output(...)`；`execution.get_result()` |
| 长任务或证据驱动任务 | 使用 AgentExecution task strategy，配置 goal、success criteria、effort、Workspace 和 `auto` strategy。 | `agent.use_workspace(...).goal(..., success_criteria=[...]).effort("medium").strategy("auto").get_result()` |
| 显式策略控制 | `direct` 用于普通 request/action execution；`flat` 用于线性 bounded task work；`taskboard` 用于 board/dependency coordination。 | `execution.strategy("direct")`；`execution.strategy("flat")`；`execution.strategy("taskboard")` |
| 面向用户的最终文本 | 从 result text facade 读取 task-strategy final text。 | `result.get_text()`；`await result.async_get_text()` |
| 结构化任务状态 | 从结构化 result/meta 读取 task status、artifact status、task refs、completion notes、diagnostics。 | `result.get_data()`；`result.get_meta()`；`result.task_refs` |
| Durable records | 通过 Workspace 写入 durable records。 | `workspace.put(collection=..., kind=..., content=..., tags=[...])` |
| Model-hot retrieval context | 用 Workspace intelligent retrieval 获取将进入 model request 或 AgentTask work unit 的 records/files。 | `await workspace.retrieve(query=..., sources=["records", "files"], budget={"chars": ...})` |
| 确定性精确搜索 | 使用 deterministic grep surfaces 做低成本精确查询和诊断。 | `await workspace.grep(...)`；`await workspace.grep_files(...)` |
| Session memory | 将 Session memory 绑定到 Workspace，并使用内置 memory plugin 管理 global/session memory records。 | `session.use_memory(mode="AgentlyMemory", workspace=workspace)`；`agent.activate_session(...)` |
| Workspace file work | 文件 read/search/edit/write 保持在 Workspace file actions 内。 | `agent.enable_coding_agent_actions(...)`；Workspace file IO handlers |
| Shell 与本地命令 | Shell 用于 tests、builds、git inspection、bounded diagnostics。 | `agent.enable_shell(...)`；bounded stdout/stderr artifacts |
| External Actions | 显式挂载 actions，并让 ActionRuntime 拥有 planning、dispatch、policy、artifacts、observations。 | `agent.use_actions(...)`；ActionRuntime records；Action artifact refs |
| Execution resources | 将 runtime capabilities 绑定为 ExecutionResources。 | `ExecutionResource`；内置 ACP、Bash、browser、Docker、MCP、Node.js、Python、SQLite providers |
| Human-in-the-loop work | 使用 ExecutionExchange 与 PolicyApproval-backed wait/approval surfaces。 | `ExecutionExchange`；`PolicyApproval`；console / host-callback exchange providers |
| Skills usage | 通过 AgentExecution/Agent APIs 选择 Skills，并让 SkillsExecutor 构建 context packs 和 capability plans。 | `agent.use_skills(...)`；Skills context packs；Skills capability policy |
| Dynamic DAG work | 使用 TaskDAG 处理 acyclic dynamic planning 和 execution。 | `TaskDAGExecutor.compile_blocks(...)`；`TaskDAGExecutor.async_run_blocks(...)` |
| Workflow orchestration | 使用 TriggerFlow 处理显式 branching、waiting、pause/resume、runtime streams、durable workflow execution。 | `Agently.create_trigger_flow(...)`；`TriggerFlow(...)`；`flow.create_execution(...)` |
| Runtime streams | `delta` 用于面向用户文本；`instant` / structured events 用于 UI state 和 diagnostics。 | `get_async_generator(type="delta")`；`get_async_generator(type="instant")`；RuntimeEvents |
| DevTools observation | 通过 DevTools 观察 AgentExecution、model requests、actions、TaskBoard progress、exchanges、telemetry。 | `agently-devtools >=0.1.10,<0.2.0`；RuntimeEvent / ObservationEvent bridge |

## 最终升级矩阵

| 领域 | 4.1.4 最终升级 | 最终推荐用法 |
|---|---|---|
| AgentExecution ownership | `AgentExecution` 拥有一次 Agent run：prompt state、action execution、task strategy、process stream、result wrapper、run metadata。 | 使用 `AgentExecution` 作为 prompt、action、Skill、task、stream、result consumption 的公开 run surface。 |
| Strategy selection | Execution strategy 收敛为 `auto`、`direct`、`flat`、`taskboard`。 | 普通工作使用 `auto` 或 `direct`；线性 bounded task work 使用 `flat`；board/dependency coordination 使用 `taskboard`。 |
| Direct route | Direct execution 保持普通 model-request 和 ActionLoop run 的轻量路径。 | 用 direct route 处理短 request/response work 和简单 ActionLoop tasks。 |
| Flat route | Flat execution 共享 AgentTask substrate，并可先将 remaining work 交给下一个 work unit，再进入 final verification。 | 用 Flat 处理需要 evidence、readback、final verification 但不需要 board scheduling 的顺序长任务。 |
| TaskBoard route | TaskBoard execution 共享 AgentTask foundations，并增加 board state、dependency state、patching、continuation、finalization、bounded projection。 | 用 TaskBoard 处理多部分 deliverables、依赖密集工作、fan-out/fan-in work 和长制品。 |
| Result text | task-strategy results 暴露 `final_response`；`get_text()` 与 `async_get_text()` 优先返回该 final response。 | 用 result text facades 获取面向用户的最终答案。 |
| Result payloads | execution result payloads 暴露 terminal status、artifact status、final result data、task refs、completion notes、diagnostics。 | 用 structured result/meta data 驱动应用状态、审计和 UI detail panels。 |
| Streams | AgentExecution streams 暴露 process events、instant items、delta text、retry boundaries、exchange state、action observations、terminal summaries。 | 从 `delta` 渲染用户文本；从 `instant` 或 RuntimeEvents 渲染结构化 UI state。 |
| Runtime context | runtime context 保留为 diagnostics；model-hot task prompts 不把具体 runtime timestamps 写入生成制品。 | 将业务日期放入 caller input 或 source evidence。 |
| Incremental acceptance | TaskBoard acceptance 携带 dirty/cache markers、card/evidence ids、verdict fingerprints、verification refs、counters、progress percent。 | 用 acceptance metadata 驱动 task status、board UI 和 verification efficiency。 |
| Verifier reuse | TaskBoard final verification 可复用未变化的 green verifier verdict，并将 dirty verifier input 限定到受影响 acceptance items。 | 让 TaskBoard 只验证变化的 acceptance areas，同时保留 final verifier authority。 |
| Setbacks | TaskBoard cards 可用 `setback` 表示可恢复的 readback、repair、patch、continuation failure。 | 将 setback 渲染为可恢复 task state，并继续执行已排程 recovery work。 |
| Final verification | final verification 接收 pinned evidence ids、normalized verifier evidence、artifact refs、readback facts、acceptance locators、completion notes、unresolved-criteria metadata。 | 使用 verifier output 与 host guards 作为最终 task acceptance path。 |
| Runtime guidance | active task-strategy execution 可接受 runtime guidance，并在下一个安全边界前存为 Workspace guidance records。 | 用 `add_guidance(...)` / `async_add_guidance(...)` 为 active task runs 追加 operator context。 |
| Evidence ledger | `EvidenceEnvelope.evidence_items` 是 Flat synthesis、TaskBoard synthesis、verifier prompts、host guards、artifact locators 的 canonical grounding ledger。 | 源依据重要时，通过 structured outputs 将 output claims 绑定到 evidence ids。 |
| Evidence binding | host guards 将 evidence handles、paths、records、URLs、artifacts、action ids、action-call ids、provenance aliases 统一到 canonical ledger ids。 | 在 structured result fields 中使用 visible evidence handles 或 canonical ids。 |
| Artifact delivery | Workspace artifact delivery 记录 write facts、readback facts、SHA-256、byte counts、previews、file refs、manifests、targeted readbacks、acceptance locators。 | 通过 Workspace files 与 readback-backed artifact refs 交付长制品。 |
| Binding repair | binding repair 面向 unresolved evidence bindings 定向修复，不重新生成完整 deliverables。 | 用 targeted repair 修复 source-binding failures。 |
| Workspace foundation | Workspace 成为 records、files、evidence links、checkpoints、runtime event storage、artifact refs、file policy metadata、retention anchors、leases、backend capability reporting 的 durable boundary。 | 将一个 Workspace 绑定到需要共享 durable context 的 Agents、TriggerFlow executions、service workers。 |
| Local Workspace backend | local backend 使用 filesystem storage 与 SQLite records、WAL、busy timeout、scope indexes、lineage-aware file roots、scoped prune。 | 用 local Workspace 支撑开发、本地 durable state、examples、filesystem-backed artifacts。 |
| Workspace writes | `workspace.put(...)` 是 canonical record-write API，并支持 `content=...` 与 profile handlers。 | 用 `workspace.put(...)` 写 records。 |
| Workspace providers | Workspace backend providers 可通过 Workspace provider seam 注册和选择。 | 通过 Workspace provider registration 注册 custom backends，并在 Agent 或 execution boundary 绑定。 |
| Workspace file IO | Workspace file IO 拥有 path containment、file refs、deterministic file info、handler dispatch、text read/write、optional export handlers、diagnostics。 | 将 file IO、export、file-action roots 保持在 Workspace 内。 |
| Intelligent retrieval | `workspace.retrieve(...)` 为 records/files 提供共享 intelligent retrieval，包含 keyword/tag candidates、optional vector/hybrid candidates、rerank、refill、budgeted packaging。 | records/files 要作为 model context 或 AgentTask evidence 时使用 `retrieve(...)`。 |
| Deterministic search | `workspace.grep(...)` 和 `workspace.grep_files(...)` 提供 records/files 的 deterministic exact search。 | 用 `grep(...)` / `grep_files(...)` 做精确查询、调试和诊断。 |
| Workspace store providers | Workspace 将 `DBStoreProvider`、`EmbeddingProvider`、`VectorStoreProvider` 拆开：默认 DB store 是 SQLite，`vector_store_provider="auto"` 会在 Chroma 可用时选择 Chroma，否则降级到 SQLite vector table。 | 通过 `db_store_provider` 接入 record DB adapter，通过 `embedding_provider` 接入向量化，通过 `vector_store_provider` 独立选择向量存储。能力较低的 DB store 保持同一协议面，对不支持的高级能力返回空值或缺省值。 |
| Session memory | `SessionMemory` 成为 plugin protocol；内置 `AgentlyMemory` 将 global/session memory 存储为 Workspace records。 | 用 `AgentlyMemory` 实现 Workspace-backed Session memory 和 scoped recall。 |
| Blocks | Blocks 将 AgentTask ExecutionPlan / PlanBlock work 和已验证 TaskDAG nodes 降级为 TriggerFlow-backed ExecutionBlockGraph。 | 让 AgentTask 和 TaskDAG 使用 Blocks 作为 runtime execution 的 lowering bridge。 |
| TaskDAG | TaskDAG 拥有 acyclic dynamic planning、validation、resolver binding、execution、retry metadata、result adaptation、evidence mapping。 | 用 TaskDAG 直接处理显式 DAG-shaped automation 和 dynamic planning。 |
| TriggerFlow | TriggerFlow 增加 durable snapshots、pause/continue、interrupt/resume ledgers、RuntimeEvent persistence、exchange metadata、compaction policy、load inspection、resource requirements、idempotent resume ids。 | 用 TriggerFlow 处理需要显式 orchestration、waits、resume、runtime streams、durable execution state 的 workflow。 |
| ExecutionExchange | ExecutionExchange 提供 approvals、decisions、control messages、clarifications、guidance、acknowledgments 的 exchange manager。 | 用 exchange providers 和 PolicyApproval-backed wait surfaces 处理 human-in-the-loop flows。 |
| ActionRuntime | ActionRuntime 拆分 action planning、dispatch、policy approval、execution、artifact management、resource binding、observation records。 | 显式挂载 actions，并从 ActionRuntime records 检查 execution facts。 |
| ExecutionResource | ExecutionResource 拥有 ACP、Bash、browser、Docker、MCP、Node.js、Python、SQLite runtimes 的 provider-backed runtime binding。 | 将 runtime capabilities 作为 resources 绑定，避免在业务代码中嵌入 provider mechanics。 |
| ACP and MCP | ACP 同时是 Action 和 `ExecutionResource(kind="acp")`；MCP-declared artifacts 通过 Action artifact refs 和 AgentTask evidence handoff 流转。 | 在 capability boundaries 启用 ACP 或 MCP，并通过 evidence/readback paths 消费 artifact refs。 |
| Workspace file actions | coding-agent Workspace actions 暴露 file read、glob、grep、edit、unified-diff patch、stale-guarded write。 | repository/file tasks 使用 Workspace file actions；tests、builds、diagnostics 使用 shell。 |
| Browse and Search | Browse 与 Search actions 使用 policy-controlled execution、fallback behavior、bounded outputs、explicit diagnostics。 | 将 Browse/Search 作为 mounted capabilities 使用，并消费 bounded output records。 |
| SkillsExecutor | SkillsExecutor 记录 capability needs、构建 context packs、发现/激活 capabilities，并暴露 TaskDAG resolver support。 | 用 `agent.use_skills(...)` 和 Skills context packs 支撑 Skill-guided AgentExecution work。 |
| Skills diagnostics | Direct Skills execution 发出 structured abort diagnostics；react/staged strategies 发出 budget-exhausted diagnostics。 | 将 Skills diagnostics 暴露到 host logs、streams 或 DevTools views。 |
| Model requesters | Model requester providers 模块化为 credential、handler、request-builder、response-adapter、transport、type、plugin modules。 | 通过 model keys、provider settings、requester plugins 配置 model providers。 |
| Model routing | Model routing 支持 layered model keys、provider fallback、API key pools、request-time key selection、provider-error retry policies。 | 用 model keys 和 pool settings 处理 provider fallback 与 key rotation。 |
| Model liveness | Model response materialization 为 first event、stream、non-streaming response、materialization stages 提供 liveness deadlines。 | 用 liveness diagnostics 定位 stalled provider stages。 |
| Stream retry status | `ModelRequestResult` 暴露 `$status` records 和 plain delta retry replay markers。 | structured stream state 消费 `$status`；plain text replay boundaries 消费 retry markers。 |
| Telemetry | Model request telemetry 记录 response ids、attempts、run ids、provider/model data、request URLs、duration、usage summaries、side-channel facts、errors、estimated input/output lengths。 | 将 telemetry 提供给 DevTools 和 host diagnostics。 |
| Structured output | output defaults 由 settings 拥有；已发布 parsers 包含 `xml_field`、`hybrid`、JSON、`yaml_literal`、`flat_markdown`；required fields 强制 meaningful values。 | model-owned structured decisions 使用 `.output(...)` 和 Agently output control。 |
| Image input | VLM helpers 可从 local files、URLs、bytes 或 structured image payloads 构建 rich image input。 | VLM input 使用 `agent.image(...)` / request image helpers。 |
| RuntimeEvent | RuntimeEvent 是 core runtime event record；EventCenter dispatches RuntimeEvents，并支持 delivery policy、coalescing、background reclaim。 | 使用 RuntimeEvents 作为统一 observation feed。 |
| DevTools | DevTools 消费 AgentExecution streams、model status、task progress、action observations、exchange states、retry status、terminal summaries、telemetry。 | Agently 4.1.4 搭配 `agently-devtools >=0.1.10,<0.2.0`。 |
| Public typing | 包内发布 `agently/py.typed`，并扩展 facades、protocols、TypedDicts、data contracts、callbacks、stream handlers、result wrappers、Workspace、ExecutionExchange、TaskBoard helpers 的 typing。 | 对安装后的 package 使用 pyright/Pylance-compatible tooling。 |
| Docs and examples | 文档和 examples 覆盖 AgentExecution strategy、Workspace retrieval、Session memory、Action Runtime、ExecutionResource、TriggerFlow lifecycle、Skills execution、DevTools observation、structured output、release workflows。 | 新 examples 从 4.1.4 AgentExecution、Workspace、TriggerFlow、Skills、ActionRuntime surfaces 开始。 |
