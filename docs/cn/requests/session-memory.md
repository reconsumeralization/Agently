---
title: 会话记忆
description: Session 如何接入 agent、记录多轮历史、控制上下文窗口并导入导出。
keywords: Agently, session, activate_session, chat history, memo, context window
---

# 会话记忆

> 语言：[English](../../en/requests/session-memory.md) · **中文**

Session 是 Agently 的多轮对话容器。它保存完整对话历史（`full_context`），同时维护真正放进下一次请求的窗口（`context_window`）。默认策略只做长度控制；如果你需要摘要、长期偏好或更复杂的裁剪逻辑，需要注册自己的 analysis / resize handler。

## 启用与关闭

```python
from agently import Agently

agent = Agently.create_agent()
agent.activate_session(session_id="support-demo")

agent.input("请记住：我的订单号是 A-100。").start()
reply = agent.input("我的订单号是什么？").start()

agent.deactivate_session()
```

`activate_session(session_id=...)` 会创建或复用这个 id 对应的 `Session`，并把 `runtime.session_id` 写进 agent settings。关闭时用 `deactivate_session()`；关闭后 agent 不再把 session chat history 注入请求。

## Workspace 持久长期记忆

需要持久记忆时，为 Session 挂载 `SessionMemory` 插件。内置样板插件是
`AgentlyMemory`；它把记忆写入 Workspace record，并在下一次请求前检索并注入相关记忆。

```python
from agently.core import Session

workspace = Agently.create_workspace("./support-memory")

session = Session()
session.use_memory(mode="AgentlyMemory", workspace=workspace)
```

由 Agent 创建的 Session 可以自动绑定当前 Agent Workspace：

```python
agent = Agently.create_agent()
agent.use_workspace("./support-memory")
agent.activate_session(session_id="support-demo")

agent.activated_session.use_memory(mode="AgentlyMemory")
```

这里配置的是普通 Workspace 根目录；应用不应把 `.agently` 本身绑定成 Workspace。
仅创建 Session、激活 Session 或绑定 Workspace 都不会创建私有状态。第一次执行基于
record 的记忆写入或查询时，才会懒创建 `.agently/workspace.db`。默认 record-only
模式不会物化向量 provider；只有确实需要向量写入或查询时才开启：

```python
agent.set_settings("session.memory.AgentlyMemory.vector_index.enabled", True)
```

`vector_index.enabled=True` 会让抽取出的记忆 record 请求向量索引；真正发生向量操作时，
才会物化已配置的 embedding 和 vector provider。

`AgentlyMemory` 写入的记忆 record 使用：

- `collection="memory"`
- `kind="global_memory"` 表示 `GLOBAL_MEMORY`
- `kind="session_memory"` 表示 `SESSION_MEMORY`
- 固定的 `provenance`、`tags`、`memory_scope` 和可选 `vector_index` 元数据

`GLOBAL_MEMORY` 在同一个 Workspace 内共享。`SESSION_MEMORY` 还会按
`runtime.session_id` 继续隔离。独立 `Session` 必须显式传入 `workspace=...`；当
Workspace-backed 记忆插件需要存储但没有可用 Workspace 时，Agently 会抛出清晰错误。

记忆 body 结构和模型 prompt 可通过 `session.memory.AgentlyMemory.*` 配置。
prompt 覆盖使用 Configure-Prompt 风格的 `.execution` block：

```python
agent.set_settings(
    "session.memory.AgentlyMemory.body_schema",
    {
        "preference": "string",
        "project": "string",
        "evidence": "short string",
    },
)

agent.set_settings(
    "session.memory.AgentlyMemory.extract.execution.instruct",
    "只抽取可长期复用的用户偏好和项目事实。",
)
```

模型负责抽取、压缩、检索 query 规划和 rerank 判断。确定性代码只负责结构校验、
Workspace 过滤、写入和预算控制。对于很小的记忆候选集，`AgentlyMemory`
会在候选数量低于 `session.memory.AgentlyMemory.retrieve.rerank_min_candidates`
时跳过 rerank（默认阈值为 `2`），并记录 `memory_rerank_skipped` 诊断。
rerank 重试后仍失败时，会降级到确定性候选并记录诊断。当某个记忆 scope 的候选
被 rerank 全部丢弃时，`AgentlyMemory` 也会保守降级：对该 scope 关闭 rerank
重新取回确定性候选、注入记忆包，并记录 `memory_rerank_empty_fallback` 诊断。可通过
`session.memory.AgentlyMemory.retrieve.keep_candidates_on_empty_rerank=False`
关闭这个保护。

## Chat history 入口

启用 session 后，agent 上这几个方法会代理到当前 session：

```python
agent.set_chat_history([
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好，我是 Agently 助手。"},
])

agent.add_chat_history({"role": "user", "content": "继续上个话题"})
agent.reset_chat_history()
```

当前 session 在 `agent.activated_session`。没有启用 session 时，上面这些方法会退回普通 agent prompt 的 chat history 行为。

## 默认窗口策略

`Session` 的默认配置是：

```python
agent.set_settings("session.max_length", 12000)
```

当 `context_window` 的近似文本长度超过 `session.max_length` 时，默认 handler 使用 `simple_cut`：从最新消息往前保留，直到窗口长度不超过限制。如果只有最后一条消息也超长，就截取这条消息的末尾。

这个长度是按消息序列化后的字符数近似，不是精确 token 计数。

## 记录哪些内容

默认情况下，请求结束后 session 会把本次 prompt 文本作为 user 内容、把结果数据作为 assistant 内容追加到历史。想只记录部分字段时，用：

```python
agent.set_settings("session.input_keys", ["info.task", "input.question"])
agent.set_settings("session.reply_keys", ["answer", "score"])
```

`session.input_keys` 从 prompt 数据里取路径；`session.reply_keys` 从解析后的结果数据里取路径。设为 `None` 时恢复默认记录方式。

## 自定义 resize / memo

框架内置的 session 不会自动调用模型生成摘要。`memo` 是一个可序列化字段，供自定义 resize handler 写入：

```python
def analysis_handler(full_context, context_window, memo, session_settings):
    if len(context_window) > 6:
        return "keep_last_four"
    return None


def keep_last_four(full_context, context_window, memo, session_settings):
    new_memo = {
        "previous_turns": len(full_context) - 4,
        "note": "Older turns were summarized by application code.",
    }
    return None, list(context_window[-4:]), new_memo


agent.register_session_analysis_handler(analysis_handler)
agent.register_session_resize_handler("keep_last_four", keep_last_four)
```

resize handler 适合管理 chat window 和 `memo` 字段。如果记忆需要持久化为
Workspace record，使用 `session.use_memory(...)`。

## 导入 / 导出

```python
from agently.core import Session

session = agent.activated_session
json_text = session.get_json_session()
yaml_text = session.get_yaml_session()

restored = Session(settings=agent.settings)
restored.load_json_session(json_text)

agent.sessions[restored.id] = restored
agent.activate_session(session_id=restored.id)
```

也可以用别名：`session.to_json()` / `session.to_yaml()`、`session.load_json(...)` / `session.load_yaml(...)`。

## 边界

Session 负责多轮 chat history、当前上下文窗口、可选 memo 字段、memory 插件挂载和导入导出。Workspace 负责持久化和检索。`SessionMemory` 插件负责记忆策略。V1 不提供跨 Workspace 的用户画像或自动同步。

## 另见

- [Context Engineering](context-engineering.md) —— 知识该放 session、prompt info 还是 KB
- [知识库](../knowledge/knowledge-base.md) —— 检索型上下文
- [Prompt 管理](prompt-management.md) —— chat history 如何进入请求
