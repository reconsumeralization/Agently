---
title: Session memory
description: Session history 与 RecordStore 持久长期记忆。
keywords: Agently, Session, memory, RecordStore, AgentlyMemory
---

# Session memory

`Session` 负责多轮 chat history、当前 context window、可选 memo、memory plugin
挂载和 import/export。它不负责持久存储，也不是通用任务上下文所有者。

内置 `AgentlyMemory` plugin 把长期记忆写入 RecordStore，并在后续请求前检索
相关 candidate。

```python
agent = Agently.create_agent("support").use_record_store(
    "./support-memory",
    mode="read_write",
)
agent.activate_session(session_id="customer-42")
session = agent.activated_session
assert session is not None
session.use_memory(mode="AgentlyMemory")
```

本地数据库按需创建在
`./support-memory/.agently/records/records.db`。TaskWorkspace 与此无关；只有
任务需要读写文件时才配置 TaskWorkspace。

`GLOBAL_MEMORY` 共享配置的 RecordStore search scope；`SESSION_MEMORY` 还包含
当前 session id。需要 user、tenant 或 project 隔离的应用，必须在 RecordStore
边界配置并强制执行 scope。

提取与检索配置位于 `session.memory.AgentlyMemory.*`：

```python
agent.set_settings(
    "session.memory.AgentlyMemory.body_schema",
    {"project": "string", "preference": "string", "evidence": "short string"},
)
agent.set_settings("session.memory.AgentlyMemory.extract.max_memories", 2)
agent.set_settings(
    "session.memory.AgentlyMemory.retrieve.budget",
    {"chars": 2000, "item_chars": 800, "rerank_candidates": 3},
)
agent.set_settings("record_store.vector_index.enabled", True)
```

SessionMemory 仍负责 extraction/compression policy 与 accepted RecordStore
写入。它把 active recall 暴露为 source kind 为 `session_memory` 的
`AgentlyMemoryContextSource`；AgentExecution 将这份 TaskContext source 与其他任务
信息一起绑定。随后由 ContextIndex 复用 structural/vector candidate，并由
ContextReader 完成 consumer-bound 精确读取与 `ContextPackage` 交付。SessionMemory
不再运行第二套 retrieval-to-prompt pipeline。

memory extraction、prose relevance、rerank 与 summary 属于模型语义工作。宿主
代码负责 schema 校验、RecordStore filters、持久化 accepted records 与预算
约束；plugin 不用关键词表充当语义所有者。

即时会话连续性使用 Session chat history；持久记忆使用
`session.use_memory(...)`；execution 需要组合 Skills、files、records 与直接
任务信息时，使用 TaskContext/ContextReader。
