---
title: Blocks 生命周期
description: ExecutionPlan、PlanBlock、ExecutionBlock、TriggerFlow、Skills、TaskDAG 与 evidence 的边界。
keywords: Agently, Blocks, ExecutionPlan, PlanBlock, ExecutionBlock, TriggerFlow, Skills, TaskDAG, evidence
---

# Blocks 生命周期

Blocks 是复杂任务执行里的内部生命周期桥接层，不是第二套公开任务 runtime。
外层任务生命周期仍归 `AgentExecution` 和 AgentTaskLoop 策略所有，TriggerFlow
仍是执行底座。

生命周期是：

```text
TaskFrame
-> 带 PlanBlock instances 的 ExecutionPlan
-> Blocks compiler
-> ExecutionBlockGraph
-> TriggerFlow execution
-> EvidenceEnvelope 与 ResultAdapter output
-> AgentTaskLoop verification 和 host guards
```

## 所有权

| 概念 | Owner | 含义 |
|---|---|---|
| `ExecutionPlan` | AgentTaskLoop / AgentExecution strategy | 当前 task frame 的一个有边界计划片段。 |
| `PlanBlock` | Blocks planning catalog | Planner 可见的能力规格，包含输入、输出、能力需求、证据合同和 runtime binding 选择。 |
| `ExecutionBlock` | Blocks runtime catalog | 可信 runtime block，会降低到一个 TriggerFlow chunk 或固定 chunk/signal 组。 |
| `ExecutionBlockGraph` | Blocks compiler output | 面向 TriggerFlow 的 lowering artifact，类似 compiled TaskDAG。 |
| `TaskDAG` | TaskDAG modules | DAG 数据、校验、依赖语义和 semantic output mapping。 |
| `TriggerFlow` | TriggerFlow | Runtime dispatch、signals、joins、concurrency、pause/resume、stream、close snapshot 和 recovery。 |
| `EvidenceEnvelope` | Blocks mapper / AgentTaskLoop | Verifier 与 deterministic host guards 使用的运行事实。 |

## Evidence Ledger

`EvidenceEnvelope` 是内部权威证据账本。它的 `evidence_items` 列表为每个可
支撑声明的 Blocks、Workspace、Action 和 readback 事实保留稳定 `id`、`kind`、
`status`、`body_state` 和 provenance。

`status` 是 `ok`、`failed` 或 `empty`。失败和空结果也是证据：它们只能支撑
不可用或缺失数据声明，不能支撑正向业务事实。`body_state=ref_only` 只能证明
URL、路径、artifact 或 record ref 被发现或物化。`bounded` 和 `truncated`
内容只能支撑可见正文；完整来源声明需要后续 readback evidence item。

Flat bounded step、TaskBoard card/final synthesis、verifier prompt 和 host guard
都从同一批 ledger id 派生热视图。兼容视图如 `scoped_retrieval_results`、
TaskBoard `source_refs` 和旧 EvidenceEnvelope buckets 仍然存在，但它们不是
独立 grounding 权威。结构化模型输出可以包含 `evidence_use` 绑定，让
deterministic guard 在 verifier 判断前拒绝无效 id、failed/empty 正向支撑和
`ref_only` 内容支撑。

模型 prompt 可以展示短 `cite_as` 句柄以及 canonical id。这些句柄、路径、
record id、URL、artifact id 和 action id 只是确定性别名；AgentTask 会在进入
verifier 前把它们归一回 ledger id。别名冲突时阻断，不猜测。

Workspace artifact write/readback 和 targeted artifact readback 都是 ledger
producer。Verifier 只能检查已经进入 ledger 的 readback 事实；它不会创建一条
合成端看不到的私有 readback 视图。TaskBoard 可以在终态卡片已经产出可信
candidate 时跳过重复 final synthesis，但 promoted result 仍必须通过同一个
ledger guard 和 terminal verifier。

## Skill Activation

Skills 是 progressive context 和 capability package。`skill_activation`
PlanBlock 可以在预算内加载被选中的 `SKILL.md` guidance 和 resource refs，推断
capability needs，并推荐下游 PlanBlocks。它不会执行 scripts，不会授予
Actions/MCP/shell/browser 访问权，也不能证明 side effect 已发生。

应用代码需要低层视图时，用当前 facade：

```python
activation = Agently.skills_executor.activate_skill(
    "incident-review",
    task="review ticket evidence",
)
```

Side-effect evidence 必须来自下游 `action_call`、`workspace_operation`、
`approval_wait` 或其他具体 execution blocks。

## 直接 Skills 兼容层

`agent.run_skills_task(...)` 仍是显式 Skills facade，但底层使用同一条 Blocks
lowering 路径。一次执行会构造内部 ExecutionPlan：每个 selected Skill 对应一个
`skill_activation` PlanBlock，另有一个具体策略 PlanBlock：

- `single_shot` 降低为 handler-backed `model_request` ExecutionBlock。
- `runtime_chain`、`staged`、`react` 和自定义 route labels 降低为
  handler-backed `flow_segment` ExecutionBlocks。

生成的 `SkillExecution.close_snapshot["blocks"]` 包含 ExecutionPlan、
ExecutionBlockGraph、TriggerFlow close snapshot、ResultAdapter output 和
EvidenceEnvelope。旧策略名应视为兼容 route labels 和 diagnostics，不是另一套
Skills-owned lifecycle。

## Runtime Blocks

Blocks 只运行可信 runtime code。`action_call`、`model_request`、`flow_segment`
和 `agent_step` 等 handler-backed blocks 需要 runtime handler。
`workspace_operation` 需要绑定 Workspace resource。`approval_wait` 使用框架
PolicyApproval / TriggerFlow pause surface。`external_wait` 使用 TriggerFlow
pause/resume。

PlanBlock 与 ExecutionBlock registry 会校验已知 block kind、可信 runtime
binding reference、signal contract，以及 resource/capability requirements。
编译时如果 plan edge 指向缺失 block、capability 被拒绝，或 pending capability
没有匹配的 `approval_wait`，也会 fail closed。

当 `approval_wait` 或 `external_wait` 打开 TriggerFlow pause 时，Blocks 会为该
block 记录 `waiting` evidence。Resume decision 仍保存在 TriggerFlow
interrupt/resume ledger 中；不要把 waiting block 当成任务终态验收。

缺少必需 handler 或 resource 时，block 会 fail closed。如果 block 发出结构化
`ReplanSignal`，Blocks 只取消被点名影响的 ExecutionBlocks 及其下游 blocks；
下一步 repair/replan decision 仍由 AgentTaskLoop 拥有。

## TaskDAG Through Blocks

`TaskDAGExecutor.compile_blocks(...)` 先用 TaskDAG validator 校验 TaskDAG，
再把校验后的 DAG nodes 降低为 `ExecutionBlockGraph`。TaskDAG 仍然拥有图校验、
dependency result wiring 和 semantic output projection。Blocks 不重复校验图，
也不接受任务完成。

```python
result = await TaskDAGExecutor({"local_handler": local_handler}).async_run_blocks(
    graph,
    graph_input={"doc": "policy"},
)
```

返回的 result 和 evidence 应作为外层 verification 输入，而不是自动业务完成。

## 示例

见 `examples/blocks/01_blocks_lifecycle_infrastructure_smoke.py`。这是可运行的
infrastructure-level 示例，展示 Skill activation、handler-backed action、
Workspace evidence、validation、ResultAdapter 和 EvidenceEnvelope；它不会把 mock
业务系统当作 model-owned success。
