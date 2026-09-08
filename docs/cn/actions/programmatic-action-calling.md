---
title: 程序化 Action 调用
description: 用 programmatic ActionRuntime 规划协议完成有边界、数据依赖、只读的 Action 作业。
keywords: Agently, Programmatic Action Calling, PTC, ActionRuntime, programmatic, TriggerFlow, TaskDAG
---

# 程序化 Action 调用

> 语言：[English](../../en/actions/programmatic-action-calling.md) · **中文**

Programmatic Action Calling（简称 PTC，程序化 Action 调用）允许模型为一轮
Action 生成一段有边界的 Python 程序。程序可以调用合格的 Actions、根据结果分支、
遍历数据，并只返回一个小型结果投影。Action 的中间值留在程序内，不需要复制进
每一轮模型上下文。

PTC 是产品简称；公开 API 的值是 `programmatic`：

```python
agent.set_action_loop(
    planning_protocol="programmatic",
    max_rounds=4,
)
```

这个设置改变的是 ActionRuntime 的规划协议。它不是 `AgentExecution` strategy，
不是新工作流引擎，也不是 TaskDAG 的同义词。默认协议仍是 `structured_plan`。

一次显式 Action 运行可以覆盖 Agent 上的配置：

```python
turn = agent.input("把合格 records 与各自当前 limit 做比较。")
records = await agent.async_get_action_result(
    prompt=turn.request.prompt,
    planning_protocol="programmatic",
)
```

单次调用参数优先于 Agent 设置。必须使用精确值 `"programmatic"`；目前没有
`"ptc"` 或 `"code_mode"` 兼容别名。

低层调用方若只使用 `generate_action_call(...)` 检查程序决策而不执行，必须
释放未执行调用，避免其 host-bound catalog lease 累积：

```python
turn = agent.input("只检查一次程序决策，不执行它。")
calls = await agent.async_generate_action_call(
    prompt=turn.request.prompt,
    planning_protocol="programmatic",
)
try:
    print(calls)
finally:
    agent.release_programmatic_action_calls(calls)
```

普通 `get_action_result(...)` / AgentExecution 路径会自动结算该 lease。

## 适用场景

当一次有边界的请求需要下列能力时，程序化规划通常更合适：

- 三次或更多相互依赖的只读调用；
- 下一次 Action 取决于前一次结果的分支；
- 对一组 records 做有界循环或 fan-out；
- 在本地完成过滤、分组、连接、排序或聚合；
- 从大体积 Action 中间值中只返回一个小型最终投影。

只有一两个小型直接调用时，优先使用 `structured_plan` 或
`native_tool_calls`。此时启动代码运行环境和生成程序通常不会带来收益。

### DSv4 Flash 并发抽检结果

可运行样例
[`4_4_programmatic_vs_structured_deepseek.py`](../../../examples/action_runtime/4_4_programmatic_vs_structured_deepseek.py)
固定 task、output schema、源数据和只读 Action families，为独立 Action 声明显式
并发，并记录真实 host Action 并发峰值。默认模型是关闭 thinking 的
`deepseek-v4-flash`。

2026-09-02 的有界验收抽检分别对 fan-out、大结果归约和局部恢复执行了一组
structured/PTC 配对：

| 观测事实 | `structured_plan` | `programmatic` |
|---|---:|---:|
| 精确业务结果 | 1 / 3 | 3 / 3 |
| fan-out 模型请求数 | 4 | 3 |
| recovery 模型请求数 | 5 | 3 |
| fan-out Action 并发峰值 | 4 | 3 |
| recovery Action 并发峰值 | 3 | 3 |

抽检中的 structured route 在 fan-out 与大结果归约上不准确，PTC 返回了精确结果；
局部恢复程序只在所有 primary 调用结束后，为失败 id 调用一次 fallback。但三组
PTC 都使用了更多 prompt/total tokens 和耗时。因此业务完成、模型轮次、token 与
延迟必须分别测量。

每种 workload 只有一组配对，这不是统计显著性结论。只有当有界运行时控制和
精确本地计算的价值足以覆盖 SDK/Docker 开销时才选 PTC；不要把它当成所有多调用
任务的自动优化。

### SDK 投影与观测计量

4.1.4.8 的 renderer v3 为每个合格 Action 保留一份 canonical exact JSON
contract，继续提供 Python callable 与返回类型，并删除第二份展开的输入
`TypedDict` 投影。required fields、描述、enum、范围、pattern、额外字段策略和
并发模式仍以 exact JSON 为权威。这是确定性的 prompt 大小优化，不代表所有模型
都会因此生成更好的程序。

在确定性验证使用的重复代表性 contract 上，单 Action SDK 从 1,527 bytes 降到
1,399 bytes，32 Action SDK 从 31,287 bytes 降到 26,292 bytes。后续获授权的模型
对比仍必须分别评价业务完成度与 token。

`action.plan_ready` 在 `decision.planning_observation` 中提供 primitive-only
观测事实：renderer 版本、合格/不合格 Action 数、SDK/contract bytes 与 program
bytes。外层 Action settle 后，`meta.programmatic_observation` 还会提供 wrapper
bytes、binding 调用结果和观测到的 active binding 并发峰值。这些字段仅用于诊断，
且只在 provider 实际上报时出现；缺失事实不会被推断成 0。它们不参与路由、授权、
retry 或 acceptance，也不会复制到下一轮模型可见 Action record。

## 当前合格边界

当前协议生成一段 Python 3.10+ async function body；它的 return value 必须符合
lossless JSON data model。

程序化模式不会暴露所有已注册 Actions，只纳入同时满足下列条件的 Action：

- 在当前运行范围内可见，且 `expose_to_model=True`；
- 声明了 `side_effect_level="read"`；
- 声明了 `replay_safe=True`；
- 没有静态要求审批；
- 有能够表示为 JSON 的明确返回 contract。

使用 `@agent.action_func` 时，请提供准确的 Python 返回类型注解；注册
executor-backed Action 时，请提供等价的 `returns=` contract。缺少返回 contract
的 Action 会被排除并产生 diagnostics；Agently 不会把它当成隐式 `Any` 返回。

写入与执行类 Action，以及安装、支付、发布、删除和等待审批的操作，都不属于
PTC V1。它们应保留为普通、图上可见的 Action，并拥有各自的审批与证据边界。

## 执行与安全

一轮程序化调用遵循下面的边界：

```text
ModelRequest 生成有边界的 Python 程序
  -> 保留的 run_action_program Action
  -> 支持 host binding 且满足隔离要求的 code ExecutionResource
  -> 每个嵌套调用重新进入 ActionRuntime 与 ActionDispatcher
  -> 有界 program return / logs 进入下一轮模型上下文
```

模型不会直接调用 `run_action_program`，应用也不应注册这个保留 Action id。
每个嵌套调用仍会经过已注册 schema、host policy、resource、timeout、结果归一化
和 Action evidence 校验。程序代码拿不到 credentials、policy override、canonical
Action call id 或 host live object。

PTC 要求一个满足 required isolation、支持 host binding 的 `code_execution`
provider。没有合格 provider 时会 fail closed，绝不会静默回退到
`trusted_local`。程序进程不能直接访问网络或 host environment；已注册 Action
仍可通过自身的正常 executor 使用 host 明确授权的网络或托管资源。
在 POSIX host 上，内置 Docker provider 及其 gVisor variant 支持 host-binding
bridge；每次 resource 变为 eligible 之前，provider probe 仍必须验证所需 capability。

嵌套 Action 默认以 `exclusive` 模式执行。host 只有在确认某个 Action 可以独立、
安全重叠时，才应在注册时显式开启并行：

```python
agent.register_action(
    name="lookup_record",
    desc="读取一条彼此独立的记录。",
    kwargs={"record_id": (str, "记录 id")},
    func=lookup_record,
    returns={"record_id": (str, "记录 id")},
    concurrency_mode="parallel",
)
```

生成的 SDK 会携带准确的 `concurrency_mode`。程序可以对彼此独立的调用使用
`asyncio.gather(...)`。host 只并发执行明确声明为 `parallel` 的 Action，并受
`action.programmatic.max_parallel_subcalls` 限制；`exclusive` Action 会等待此前
工作完成、独占执行，并阻止后续调用提前启动。仅有 `read` 与 `replay_safe`
绝不等同于可以安全并发。

只有程序的有界 `print(...)` 输出和 JSON-compatible return value 会组成外层
Action result。嵌套 Action records 仍是 canonical evidence 与 observation data；
除非程序明确返回有界投影，它们的完整值不会进入后续 model-hot context。

## PTC、TaskDAG 与 TriggerFlow 怎么选

如果细粒度 Action DAG 只是为了表达短时工具控制和本地数据处理，PTC 可以替换
这一层微观 DAG；它不能替换负责业务 lifecycle 的图。

| 需要解决的问题 | 选择 |
|---|---|
| 一轮有边界 Action 内的数据依赖只读调用 | `programmatic` Action planning |
| 提交式或模型生成的 DAG，且执行前必须验证 | TaskDAG / DynamicTask |
| 应用拥有的稳定阶段、分支、fan-out、join 或 intervention | TriggerFlow |
| 审批、外部等待、持久化、局部重跑、补偿或跨重启恢复 | TriggerFlow / TaskDAG 宏观阶段 |

常见组合是：粗粒度 TriggerFlow 或 TaskDAG 的一个 node 内执行有边界 PTC
segment，随后把 host validation 和任何不可逆 Action 放在独立、图上可见的阶段。

`DAGActionFlow` 仍受支持。程序化模式不会 deprecate 它，也不会 deprecate
TaskDAG、DynamicTask 或 `TriggerFlowActionFlow`。

## 恢复与部分结果

运行中的 Python interpreter、正在等待的 binding，以及 provider IPC channel 都是
live resources，不是可序列化的 workflow state。TriggerFlow 可以在程序开始前、
程序 settled 后保存，但不能从程序中间恢复 live interpreter。

如果某个嵌套调用临时需要审批，或 host policy 在运行期间发生变化，Agently 会
记录正常 subcall result，并让当前程序 settle。任何 durable approval 都必须位于
这个已结束边界之外；恢复后，应基于当前 Action catalog 与 policy 重新生成程序。

后续程序代码失败时，已经完成的只读调用不会回滚。请把返回 records 和有界
diagnostics 当成部分执行证据，而不是自动业务验收结论。

## 延伸阅读

- [Action Runtime](action-runtime.md)——Action 注册、规划、派发与证据
- [ExecutionResource](execution-environment.md)——托管 code runtime 选择与隔离
- [TaskDAG / Dynamic Task](../dynamic-task/README.md)——经过验证的 DAG data
- [TriggerFlow 概览](../triggerflow/overview.md)——应用拥有、可持久化的编排
