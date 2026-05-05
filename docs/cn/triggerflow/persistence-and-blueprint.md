---
title: 持久化与 Blueprint
description: execution 状态的 save / load，flow 定义的 save_blueprint / load_blueprint。
keywords: Agently, TriggerFlow, save, load, blueprint, persistence, durable
---

# 持久化与 Blueprint

> 语言：[English](../../en/triggerflow/persistence-and-blueprint.md) · **中文**

两条独立的序列化路径，不要混淆。

| 方法 | 序列化什么 | 典型用途 |
|---|---|---|
| `execution.save()` / `execution.load(saved)` | 一次 **execution** 在某个时刻的运行时 state | 跨进程重启恢复，交给另一 worker |
| `flow.save_blueprint()` / `flow.load_blueprint(blueprint)` | **flow 定义**的结构（chunk、分支、条件） | 把 flow 当配置 artifact 分发或版本控制 |

## Execution save / load

`save()` 捕获恢复 execution 所需的全部内容：

- execution 的 `state`
- lifecycle metadata（status、时间戳、run id）
- pending interrupt state（如果碰到了 `pause_for(...)`）
- `resource_keys` —— 恢复时期望的 runtime resource 名，但不含 live 值

它**不**捕获：

- live `runtime_resources` 本体（不可序列化；见 [State 与 Resources](state-and-resources.md)）
- 在途 chunk（不存在协程中段；在稳定状态保存）

```python
execution = flow.create_execution(auto_close=False)
await execution.async_start("refund request")

saved_state = execution.save()
# 把 saved_state 持久化到某处（Redis、DB、文件等）
```

后续恢复（可能是另一个进程）：

```python
restored = flow.create_execution(
    auto_close=False,
    runtime_resources={"db": new_db_client, "logger": new_logger},
)
restored.load(saved_state)

# 继续：emit、continue_with interrupt，再 close
await restored.async_emit("UserFeedback", {"approved": True})
snapshot = await restored.async_close()
```

flow 定义两端必须一致（或兼容）—— `load()` 不会从 `saved_state` 重建 chunk 图，要求 flow 已存在。

### 跨 pause_for 的恢复

```python
execution = flow.create_execution(auto_close=False)
await execution.async_start("topic")

# 此时 flow 可能已调 pause_for(...)
saved = execution.save()

# 几天后，另一 worker
restored = flow.create_execution(
    auto_close=False,
    runtime_resources={"search_tool": new_search_function},
)
restored.load(saved)

interrupt_id = next(iter(restored.get_pending_interrupts()))
await restored.async_continue_with(interrupt_id, {"approved": True})
snapshot = await restored.async_close()
```

`get_pending_interrupts()` 返回 `pause_for(...)` 创建的 interrupt id 集合。`continue_with(id, payload)` 恢复对应挂起的 chunk。

## Flow blueprint save / load

blueprint 序列化 flow 的**结构** —— chunk 引用、分支、条件 —— 但不含 chunk 函数体（仍在代码里）。

```python
def upper(data):
    return str(data.input).upper()

def store(data):
    return data.async_set_state("output", data.input)

source = TriggerFlow(name="source")
source.register_chunk_handler(upper)
source.register_chunk_handler(store)
source.to(upper).to(store)

blueprint = source.save_blueprint()  # dict，可 JSON / YAML 序列化
```

另一端恢复：

```python
restored = TriggerFlow(name="restored")
restored.register_chunk_handler(upper)   # 同名函数体必须可用
restored.register_chunk_handler(store)
restored.load_blueprint(blueprint)
```

关键约束：blueprint 用到的 chunk 必须在恢复端**按相同 handler 名注册**。没 `register_chunk_handler(...)` loader 无法把名映回函数，load 失败。

### 何时用 blueprint

- 用 YAML / JSON 配置声明式作 flow 并在启动时 load。
- 把 flow 结构与 handler 代码分开版本管理。
- 把 flow 分发到多个已经有 chunk 实现的 worker。

### 何时**不**用 blueprint

- 一次性脚本。直接 Python 写 flow。
- 与没有 handler 代码的消费者共享。Blueprint 不自包含。

## save vs save_blueprint 对照

```text
Flow 定义（chunk、分支、条件）
        │
        ├── save_blueprint()  →  描述图结构的 dict
        │
        ▼
   create_execution()  ────►  一个 Execution
                                  │
                                  ├── save()  →  描述该 execution 状态的 dict
                                  │
                                  ▼
                              async_close() → close snapshot
```

两条路径都返回 JSON 友好 dict。存储后端（Redis、Postgres、S3、文件）由应用层选 —— 框架不带后端。

## 实用模式

**单服务器恢复**

```python
saved = execution.save()
redis.set(f"flow:{exec_id}", json.dumps(saved))

# 后续
saved = json.loads(redis.get(f"flow:{exec_id}"))
restored = flow.create_execution(auto_close=False, runtime_resources={...})
restored.load(saved)
```

**分布式 worker 拉起**

把 blueprint（存一次）和 execution save（每个 execution 存一份）配对：

```python
blueprint = source_flow.save_blueprint()
db.save("flow_blueprints", blueprint_id, blueprint)

# worker 中
flow = TriggerFlow(name="loaded")
register_all_handlers(flow)            # 你的注册入口
flow.load_blueprint(db.load("flow_blueprints", blueprint_id))

execution = flow.create_execution(auto_close=False, runtime_resources=...)
execution.load(saved)
```

## 另见

- [Lifecycle](lifecycle.md) —— 什么算「稳定可保存」的 execution
- [Pause 与 Resume](pause-and-resume.md) —— `pause_for` / `continue_with`，最常见的保存场景
- [State 与 Resources](state-and-resources.md) —— 什么存活、什么必须重新注入
