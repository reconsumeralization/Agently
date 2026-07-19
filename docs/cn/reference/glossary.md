---
title: 术语表
description: Agently 术语参考，含 seal、runtime resources、ensure、action runtime 三层等较新概念。
keywords: Agently, 术语表, lifecycle, runtime resources, ensure
---

# 术语表

> 语言：[English](../../en/reference/glossary.md) · **中文**

按字母顺序排列。如果某个术语相对旧文档发生了语义变化，词条里会指出。

## Action Runtime

Agently 三层 Action 栈的中间层：`TriggerFlow`（顶，编排）→ `ActionRuntime`（规划 + 派发）→ `ActionExecutor`（原子后端执行）。`ActionFlow` 是 runtime 与 flow 之间的桥。

`ActionRuntime`、`ActionFlow`、`ActionExecutor` 是当前的公开 plugin type。旧的 `ToolManager` plugin type 仅作为遗留兼容保留并发出 deprecation 警告。详见 [Action Runtime](../actions/action-runtime.md)。

## auto_close / auto_close_timeout

TriggerFlow execution 的设置。`auto_close=True`（默认）时，execution 在空闲超过 `auto_close_timeout` 秒后自动关闭。隐式 execution 语法糖（`flow.start()` / `flow.async_start()`）默认 `auto_close_timeout=0.0`。`flow.start(auto_close=False)` 是非法用法，会直接报错。

## Close snapshot

`execution.close()` / `execution.async_close()` 返回的 dict，封装该 execution 的最终 state。如果通过已弃用的 `set_result()` 或 `.end()` 写入了兼容结果，它会以 `"$final_result"` key 出现在 snapshot 里。详见 [Lifecycle](../triggerflow/lifecycle.md)。

## CodeExecution

执行源码的 provider-neutral Action contract。`CodeRuntimeAdapter` 校验语言输入并生成
不可变 `CodeExecutionBundle`；`TaskWorkspace` 落地 bundle 并签发 scoped access grant；
`ExecutionResource` 选择和绑定 Docker 或显式授权的 `trusted_local` 等合格 provider。
provider 不拥有源码准备或 TaskWorkspace policy。详见
[执行环境](../actions/execution-environment.md)。

## ContextReader

绑定 consumer、model 与 phase 的 `TaskContext` reader。它负责候选收集、结构过滤、
可选的结构化语义选择、渐进披露、预算、去重与不可变 `ContextPackage` 构造；不修改
source、不安装 Skill、不执行 Action，也不决定任务完成。

## ensure（第三槽） {#ensure-third-tuple-slot}

`(TypeExpr, "description", True)` 中第三槽是 `ensure` 标记——表明该叶子的路径/key 必须出现。带 `ensure` 的叶子会被编译进 `ensure_keys`（含数组通配如 `resources[*].url`）。YAML / JSON 形式：`$ensure: true`。只有当该路径还必须包含可用值时，才使用 `(TypeExpr, "description", "not_null")` 或 `$ensure: "not_null"`。

它**不是**默认值。旧的「第三槽 = default value」约定已不再支持，YAML 里也不再支持 `$default`。详见 [Schema as Prompt](../requests/schema-as-prompt.md)。

## Execution

TriggerFlow 的一次执行实例。由 `flow.create_execution(...)` 创建。生命周期状态：`open → sealed → closed`。

## ExecutionResource

operation 或 execution 使用的托管 live dependency，以 resource `kind` 表达需求，由具有
稳定 `provider_id` 的具体 provider 满足。manager 负责 probe、policy、ensure、health、
复用、release 与有序 provider 选择。它不是 sandbox 的同义词；隔离只是部分 provider
可能提供的一项 capability。

## flow_data

Flow scope 的共享数据。调用 `get_flow_data(...)` / `set_flow_data(...)` 等会触发 `RuntimeWarning`，因为该值在 flow 的所有 execution 之间共享。`execution.save()` 会包含它的序列化副本；`load()` 会用该副本替换目标 flow 对象当前的共享值，但不提供 execution-local isolation、CAS、merge 或并发安全。明确希望共享时可传 `no_warning=True` 关掉警告。execution-local 数据请用 `state`。

## Hidden execution sugar（隐式 execution 语法糖）

`flow.start()` / `flow.async_start()` 内部创建一次性 execution、跑到 close、返回 close snapshot。仅用于有限、自闭合且调用方不需要 execution handle 的运行；脚本、测试和有界服务请求都可以满足该条件。pause/resume、外部 emit、save/load、intervention、inspection、cancellation 或由 host 控制 close 的场景必须使用显式 execution。边界是生命周期控制需求，不是“脚本还是服务”。

## OpenAICompatible / AnthropicCompatible

三个协议层 Model Request 插件：`OpenAICompatible`、`OpenAIResponsesCompatible`、`AnthropicCompatible`。多数 Chat Completions 兼容 provider 配置 `OpenAICompatible`；Responses API 形态用 `OpenAIResponsesCompatible`；Claude 配置 `AnthropicCompatible`。详见 [模型概览](../models/overview.md)。

## Runtime resources

Execution-local 的活对象存储——数据库 client、回调、socket、函数指针、cache 句柄。Runtime resources **不**可序列化、**不**进 close snapshot，也**不**进 save/load execution snapshot；只记录 `resource_keys`。`load()` 后调用方必须重新注入。

这是和 `state`、`flow_data` 不同的第三类概念。详见 [State 与 Resources](../triggerflow/state-and-resources.md)。

## Runtime stream

每个 execution 一条流，由 chunk 通过 `data.put_into_stream(...)` / `data.async_put_into_stream(...)` emit；通过 `execution.get_runtime_stream(...)` / `execution.get_async_runtime_stream(...)` 消费。该流在 `execution.close()` 时关闭。

## SkillLibrary

真实世界 Skill package 的安装事实 owner，负责 discovery、validation、不可变 revision、
trust state、resource graph 与精确 resource read。Skill guidance 通过 `TaskContext` source
进入任务；获授权的 Skill script 绑定为普通 Workspace-backed CodeExecution Action。
SkillLibrary 不选择任务 route，也不“执行 Skill”。详见
[SkillsExecutor 迁移](../development/skills-executor.md)。

## seal / sealed

中间生命周期状态。`execution.seal()` / `execution.async_seal()` 拒收新外部事件，但允许已接受事件、内部 emit 链与已注册 task 继续 drain。它**不**关 runtime stream，也**不**冻结 close snapshot——后两者发生在 `close()`。

## Schema as Prompt

Agently prompt 侧结构化 authoring 的当前命名方式：嵌套 dict + 叶子 `(TypeExpr, "description", True)`，第三槽是 `ensure`。旧版「Agently DSL」试图把 `.output()`、TriggerFlow contract、外部 schema 统一成同一 IR 的方向已归档。

## state

Execution-local、可序列化、可快照的数据面。推荐入口：`data.async_set_state(...)` 写入、`data.get_state(...)` 读取。State 是 close snapshot 的来源，也是 `save()` / `load()` 往返的主体。

## TaskContext

一个任务可用信息的 revisioned aggregate。它绑定直接条目，以及 SkillLibrary、
TaskWorkspace、RecordStore、memory、evidence 和已授权 external source 等 adapter；
`ContextReader` 从中生成面向具体 consumer 的 ContextPackage。TaskContext 不拥有 source
存储、文件修改、执行或任务 continuation。

## TaskWorkspace

任务的普通文件边界，负责 contained file operation、mutable working copy、artifact、
source-local search、物理 readback、稳定 file identity 与 scoped execution grant。它与
TaskContext 信息管理、RecordStore durability 是不同职责。详见
[TaskWorkspace 与 RecordStore](../requests/workspace.md)。

## TriggerFlow

编排层。负责分支、并发、批处理、循环、子流、暂停恢复、持久化、runtime stream。位于 Action Runtime 之上、应用代码之下。

## $final_result

被 deprecated 的 `set_result()` / `.end()` 写入的保留 state key。它出现在 close snapshot 里说明存在一份兼容结果。新代码应直接依赖 snapshot 本身，不要依赖这个 key。详见 [TriggerFlow 兼容](../triggerflow/compatibility.md)。

## wait_for_result=

`flow.start()`、`flow.async_start()`、`start_execution()`、`execution.start()` 等接口上 deprecated 的参数。值现在被**忽略**并发 warning；返回值形态由 `auto_close` 与「隐式语法糖 vs 显式 execution」决定。
