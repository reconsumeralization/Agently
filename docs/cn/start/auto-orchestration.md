# Agent 自动编排

Agently 4.1.3 将 `agent.start()` 作为 Agent turn 的默认用户层入口。它仍然返回
业务结果，但 Agent 可以在显式注入候选能力后，路由到普通模型响应、Actions、
Skills Executor 或 Dynamic Task。

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

候选注入是边界。如果没有注册 Actions、Skills、Skills Packs 或 Dynamic Task
候选，`agent.start()` 仍然是普通模型请求。

已验收开发线的路由是候选驱动、确定性优先：submitted Dynamic Task 候选优先，
required Skills 候选进入 Skills route。当同时存在多个可选候选，例如 auto Dynamic
Task、model-decision Skills 和普通 Actions 时，默认由模型选择 route；如果只有一个
可选候选，则直接选择该 route。

公开 Agent API 仍由 core 持有，但路线规划和执行由 active
`AgentOrchestrator` plugin 通过 `AgentOrchestrator` protocol 承担。这样
Skills、Dynamic Task 和后续 route 实现都可以替换，而不需要 core 知道内置
plugin 的内部实现。

## Execution 对象

当调用方需要路线诊断、多种结果视图或过程流式输出时，使用
`agent.create_execution()`：

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

execution 对象沿用模型 response 的消费风格：`get_data`、`get_text`、
`get_meta`、`get_generator` 以及对应 async 方法。

## 提交式 Dynamic Task 输入

提交式 Dynamic Task DAG 的 task `inputs` 继续使用 DAG 运行时占位符，例如
`${INPUT.ticket}` 和 `${DEPS.lookup}`。在 Agent route 里，graph input 按以下顺序
解析：

```text
use_dynamic_task(graph_input=...)
> execution prompt snapshot 的 input slot
> {"target": task_target}
```

因此普通 Agent prompt 写法可以直接喂给提交式 DAG，而不需要另造一套映射面：

```python
execution = (
    agent
    .use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)
    .input({"ticket": "TICKET-OK"})
    .create_execution()
)
```

prompt snapshot 在 `create_execution()` 时冻结。后续再调用
`agent.input(...)` 不会改变已经创建的 execution。只有当 DAG 输入需要区别于
Agent prompt input，或调用方想显式声明优先级时，才传
`graph_input=...`。

## Skills 语义

`agent.use_skills(...)` 和 `agent.use_skills_packs(...)` 注册 route candidates。
它们默认不再表示“把完整 Skill guidance 注入普通模型请求”。完整 Skill guidance
属于真正规划或执行该 Skill 的 Skills route。如果路由没有选中 Skills，普通请求
只接收安全的能力摘要。

如果调用方必须强制执行 Skills，使用 `agent.run_skills_task(...)`。

## 过程流

Agent execution stream item 保留熟悉的 instant stream 形态：

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

Executor route 会桥接 TriggerFlow runtime stream 和 ModelRequest instant
checkpoint，让服务能流式输出 route decision、plan/graph readiness、
task/action 进度、选定模型字段 delta 和最终 semantic outputs。
如果 TriggerFlow-backed route 失败，Agent execution stream 会关闭，并把原始
错误抛给消费者，而不是让 `get_async_generator(...)` 一直等待后续 item。

Dynamic Task 的 model 节点会把结构化输出字段映射到稳定 path：

```python
async for item in execution.get_async_generator(type="instant"):
    if item.path == "task_dag.tasks.reply.fields.reply" and item.delta:
        print(item.delta, end="", flush=True)
```

这样保留 ModelResponse `instant` 的字段级 delta 语义，同时让过程流 path
由 Agent execution route 统一拥有。
