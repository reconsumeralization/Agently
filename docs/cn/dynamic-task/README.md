# TaskDAG 与 Dynamic Task

`TaskDAG` 是 Agently 的 DAG 基石能力，面向模型或应用生成的 task graph。它拥有图
数据契约、planner、validator、resolver、executor、handler binding、dependency
results、semantic outputs 和 runtime placeholders。TriggerFlow 是更底层的执行基座。

`DynamicTask` 是当前覆盖这套 DAG substrate 的兼容与便利 facade。普通应用代码需要
一个紧凑入口时可以使用它，但它不是 `AgentExecution` 之外的第二套推荐任务生命周期。

```python
task = Agently.create_dynamic_task(target="review policy")
result = await task.async_start()
```

调用方已经有计划时，直接传入 `TaskDAG`，跳过模型规划：

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

高级调用方可以把同一条 DAG 路径拆成独立模块，按需定制，再把 DAG snapshot 作为
evidence 传给后续 `AgentExecution` 做总结、校验或下一步处理：

```python
from agently.builtins.plugins import AgentlyTaskDAGPlanner
from agently.core import TaskDAGExecutor, TaskDAGResolver, TaskDAGValidator

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

snapshot = await TaskDAGExecutor(resolver, validator=validator).async_run(
    graph,
    graph_input={"goal": goal},
)

execution = agent.create_execution()
execution.input({"goal": goal, "dag_snapshot": snapshot})
result = await execution.async_start()
```

提交式 DAG 的 `inputs` 可以用占位符引用运行时数据。整个字符串就是占位符时会保留原始值类型；占位符嵌在普通字符串里时会渲染成字符串。Slot 名大小写不敏感，但文档推荐大写：

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

`${INIT}` 指向提交 DAG 时传入的 graph input / 初始 execution input。
`${DEPS...}` 指向已完成的上游依赖结果。`${STATE...}` 读取 execution state，例如
`${STATE.task_results.lookup}`。`${TRIGGER...}` 指向原始 TriggerFlow trigger
payload（`data.value`），主要用于高级调试或 executor 层集成。运行时路径缺失会在任务执行时 fail closed，而不是把未解析字符串继续传给 handler。

当提交式 DAG 通过 `Agently.create_dynamic_task(...).async_run(...)` 运行时，
`${INIT...}` 读取传给 `async_run` 的 `graph_input`。如果没有提供
`graph_input`，DynamicTask 回退到目标 payload `{"target": task_target}`。
AgentExecution 不再拥有 DynamicTask route，因此 `Agent.use_dynamic_task(...)`
和 `AgentExecution.use_dynamic_task(...)` 会 fail fast 并给出迁移诊断。

如果 `create_dynamic_task(..., output_schema=..., ensure_keys=...)` 为 semantic
output 的 model 节点提供了前台结构契约，这个宿主契约优先于 planner 在节点上选择的
不兼容格式。多字段结构化契约遇到 planner 写出的
`inputs.output_format="flat_markdown"` 时，会被纠正回 `auto`，让输出解析器选择兼容的结构化格式。

提交式计划也可以保存成 YAML 或 JSON 配置。先把配置加载成 `TaskDAG`，再走同一个
facade：

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

`TaskDAG.from_json(...)` 接受文件路径或 JSON/JSON5 原始内容。`from_yaml(...)` 和
`from_json(...)` 都支持 `task_dag_key_path="plans.review"`，用于从较大的配置文件里选择
某一个 DAG。使用 `graph.get_yaml(path)` 或 `graph.get_json(path)` 可以导出归一化后的图。

当前 DAG workflow 代码优先使用 `Agently.create_dynamic_task(...)`。旧的
`agent.create_dynamic_task(...)` 兼容 facade 仍保留给 prompt snapshot 调用方，但
新示例应让 DynamicTask 与 `agent.start()`、`agent.async_start()`、
`AgentExecution.async_start()` 保持分离。显式传入的
`create_dynamic_task(target=..., output_schema=..., output_format=...)` 参数定义
facade 层 model-task 默认值。

模型任务应复用 Agently request 的输出流水线，不要在 handler 或 example 里自行解析
模型文本。`output_schema` 会作用到 semantic output 模型节点；如果某个模型节点
需要独立契约，可以在该节点的 `inputs.output_schema` 覆盖。每个模型任务也可以设置
`inputs.output_format`：

- `json`：紧凑的机器控制输出、Action 参数、路由标记、数字/布尔事实、model judge、密集嵌套数组/对象、严格抽取。
- `flat_markdown`：legacy section-header prompt 的显式兼容模式。
- `hybrid`：长 prose/code 字段混合 typed list/object/boolean/number 字段时的 auto 默认目标，也可显式指定。
- `xml_field`：扁平纯字符串 dict schema 的 auto 默认目标，也可显式指定。它使用 Agently 自定义 boundary parser，不是严格 XML。
- `yaml_literal`：显式 YAML target document，用于偏好 YAML 且能接受缩进敏感性的场景。
- `auto`：接受结构化 schema 自动选择输出格式，并且可以接受重试延迟。

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

提交式 DAG 可以把任务级策略放在模型节点自身：

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

提交式 DAG placeholder 使用和 Prompt reference 一致的大写命名风格，但它属于
TriggerFlow runtime 命名空间，不是 Prompt slot reference。`${INIT.foo}` 指向
初始输入，`${DEPS.task.path}` 指向已完成的上游依赖结果，
`${STATE.task_results.task.path}` 指向 execution state，`${TRIGGER.result}` 指向原始
TriggerFlow trigger payload。在 DAG task `inputs` 里，整个字符串就是 placeholder
时会保留原始运行时值类型；placeholder 嵌在普通字符串里时会转成字符串拼接。

## 架构

DAG 能力拆成四段：

- `AgentlyTaskDAGPlanner` 用 Agently output schema、`ensure_keys` 和校验重试生成确定性的 `TaskDAG`。
- `TaskDAGValidator` 校验 DAG 语法、依赖、schema version、semantic outputs、副作用策略和 resolver 可用性。
- `TaskDAGResolver` 按 `task.binding`、`task.id`、`task.kind` 的顺序解析可执行 handler。
- `TaskDAGExecutor` 把校验后的 DAG 编译为普通 TriggerFlow chunk，并复用 TriggerFlow lifecycle、stream、pause/resume、result 和 runtime resources。

`TaskDAGExecutor.async_run(...)` 是默认的 TaskDAG→TriggerFlow 直接路径，Blocks
不是必经层。只有调用方需要 `ExecutionBlockGraph`、Blocks lifecycle evidence 或
evidence/result adapter 输出时，才显式选择 `compile_blocks(...)` 或
`async_run_blocks(...)`；该路径上仍由 TaskDAG 拥有 validation 与依赖语义。

`bindings` 不再是 public facade。自定义本地函数使用 `handlers`。任务确实需要资源时再显式传入 `planner`、`model`、`actions`、`skills`；
`actions` 和 `skills` 不会在调用方未传入时暴露给 planner。

## Resolver 语义

自定义 handler 应使用清晰且以 `_handler` 结尾的名字：

```python
task = Agently.create_dynamic_task(
    target="review policy",
    plan=task_dag,
    handlers={"risk_check_handler": risk_check_handler},
)
```

DAG 中这样引用：

```python
{"id": "check_risk", "kind": "local", "binding": "risk_check_handler"}
```

未知的可选 handler 如果不影响 required semantic output、必要下游节点、审批或副作用策略，Validator 可以安全剪枝。被剪枝节点必须写入 `diagnostics`；未知的必需 handler 会在执行前 fail closed。

## 低层控制

框架集成需要分阶段控制时，再使用低层类：

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

Executor 不依赖 Agent。模型和 Action 访问由 facade 或 resolver adapter 承接；
TriggerFlow 是更底层的执行基座，不是 DAG owner API。

## 示例

`examples/dynamic_task/` 按层次提供示例：

- `01_dynamic_task_basic.py`：只使用本地 handler 的 submitted `TaskDAG`
  smoke 示例。
- `02_support_response_module_model.py`：模型驱动的智能客服回复模块，外层是
  `SupportResponseModule.respond(ticket)`，内部是 fan-out/join DAG，模型节点
  使用结构化输出，业务系统查询用 mock，并打印面向客户的回复。
- `03_contract_risk_review_business.py`：合同风险审查业务落地示例，外层是
  `ContractRiskReviewService.review(contract)`，内部组合确定性 local handler、
  后台风险评分和模型生成的前台风险 memo。
- `04_incident_briefing_auto_plan.py`：自动规划的事故简报示例，外层是
  `IncidentBriefingService.brief(report)`。模型先生成 `TaskDAG`，再由 DAG
  validator 和 executor 校验并执行；前台简报结构由 Agently `output_schema` 保证。
- `05_enterprise_renewal_complex_auto_plan.py`：复杂自动规划的企业续约示例。
  模型 planner 会生成多个独立分析 root、汇总 join 阶段，并产出结构化续约
  recovery package。
- `06_dynamic_task_config_plan.py`：通过 `TaskDAG.from_yaml(...)` 从 YAML 配置加载
  submitted `TaskDAG`。
