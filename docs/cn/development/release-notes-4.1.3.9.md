---
title: Agently 4.1.3.9 Release Notes
description: Agently 4.1.3.9 的 Workspace retrieval、SessionMemory、AgentTask scoped retrieval、向量索引接缝和公开 typing 加固说明。
keywords: Agently, release notes, 4.1.3.9, Workspace, retrieval, SessionMemory, AgentlyMemory, AgentTask, typing
---

# Agently 4.1.3.9 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.9.md) · **中文**

Agently 4.1.3.9 把 Workspace 推进为 records、files、AgentTask scoped evidence
和 Session memory 共用的持久召回底座。确定性原始搜索仍然保留，同时新增
`workspace.retrieve(...)`，用于预算化智能召回、可选向量候选、模型 rerank、refill
和紧凑的模型热路径打包。

本版本也发布可插拔 `SessionMemory` 协议和内置样板插件 `AgentlyMemory`。
HITL approval / suggestion / guidance / insertion 不属于 4.1.3.9，本批原定未完成目标
移动到 4.1.4 开发线。

## 推荐用法

调用方需要便宜、确定、精确的搜索时，用 deterministic search。检索结果要进入下一次
模型请求或 AgentTask work unit 时，用 `retrieve(...)`：

```python
results = await workspace.retrieve(
    query="What should this task remember about the customer?",
    tags=["preference", "project"],
    scope={"memory_scope": "SESSION_MEMORY"},
    sources=["records", "files"],
    budget={"chars": 12000},
    selection="length",
)

refs = await workspace.grep("deadline", filters={"collection": "memory"})
hits = await workspace.grep_files("deadline", path="notes")
```

持久 Session memory 是挂在 Session 上、由 Workspace 承载的插件：

```python
session = Session()
session.use_memory(mode="AgentlyMemory", workspace=workspace)

agent = Agently.create_agent().use_workspace(workspace)
agent.activate_session(session_id="support-demo")
agent.activated_session.use_memory(mode="AgentlyMemory")
```

Embeddings 仍然属于应用或插件逻辑。backend 有真实 `vector_index` 时，Workspace
可以使用它；没有真实索引时，vector retrieval 会带 diagnostics 降级。

```python
from agently.core.workspace import LocalVectorIndex

vector_index = LocalVectorIndex(embedder, similarity="cosine")
```

## 核心变动

| 领域 | 变动内容 | 推荐用法 | 兼容性 / 风险 | 证据 |
|---|---|---|---|---|
| Workspace retrieval | 新增 `workspace.retrieve(...)`，作为 records 和 files 共用的智能召回表面。它会构建 keyword/tag 候选，可包含文件候选，有非空 `vector_index` 时可使用 vector/hybrid 候选，按结构 gate 决定模型 rerank，补回被丢弃候选，并按长度预算或 `top_n` 打包。 | 模型热路径上下文和 AgentTask scoped evidence 使用 `retrieve(...)`。默认使用 `selection="length"`；如果数量比总文本预算更重要，再用 `top_n`。 | Retrieval 命中只是 evidence snippet 或 ref，不是完成证明。failed/empty evidence 只能支持缺失数据声明。 | `tests/test_cores/test_workspace.py`、`docs/*/requests/workspace.md`、`spec/experiments/agent-task-workspace-retrieval/round-009/`。 |
| 确定性搜索 | `workspace.grep(...)` 和 `workspace.grep_files(...)` 是明确的低成本确定性表面。`workspace.search(...)` 和 `workspace.search_files(...)` 保持兼容返回形态，底层可在确定性路径和 retrieval-backed 路径之间自动选择。 | 调试、精确过滤和便宜搜索使用 `grep(...)` / `grep_files(...)`。兼容优先时保留 `search(...)` 调用。 | search alias 默认不会触发模型 rerank。 | Workspace 兼容测试和文档。 |
| Record 打包 | 被选中的结构化 records 使用 representation packaging。`record_representation="auto"` 保留短结构，同时从模型热 snippet 中移除 cold fields；长或噪声 record 会被投影，并记录 projection diagnostics 与 raw readback refs。 | 常规 retrieval 保持 `auto`。只有下游模型热视图确实需要原始结构时，才强制 `budget={"record_representation": "raw"}`。 | 原始 Workspace record 仍然是 readback 事实源；模型热投影只是紧凑视图。 | Workspace projection 测试和 round-009 retrieval 实验。 |
| 向量接缝 | 新增 provider-neutral `LocalVectorIndex(embedder, similarity="cosine" | "dot" | "l2")`。只有配置或策略表达向量偏好，且 backend 有非空 index 时，Workspace 才会选 hybrid/vector 候选。 | embedding provider 留在业务代码、自定义 backend 或插件里。通过 vector index 传入 Workspace/backend，不把 provider 设置写进框架。 | 默认 local backend 仍是 `NoopVectorIndex`；没有真实 index 的 vector 请求会诊断降级。 | vector-mode 测试、installed-package smoke、文档。 |
| Rerank policy | 候选策略和 rerank 分离。默认 rerank 是结构 gate：只有宽查询、噪声候选、跨 records/files 或混入 distractor 时才发起模型 rerank。 | 默认用 `rerank=None`；不想花模型成本时用 `rerank=False`；调用方明确需要语义裁剪时才用 `rerank=True`。 | rerank 重试后仍失败时，Workspace 降级为确定性顺序并记录 diagnostics。 | rerank/drop/refill 测试和实验报告。 |
| Session memory | 新增 `SessionMemory` 协议和内置 `AgentlyMemory` 插件。`AgentlyMemory` 把记忆写入 Workspace：`collection="memory"`，`kind="global_memory"` 或 `kind="session_memory"`，并注入 `GLOBAL_MEMORY` / `SESSION_MEMORY`，固定记录 provenance、tags、scope 和可选 vector metadata。 | 使用 `session.use_memory(mode="AgentlyMemory", workspace=workspace)`，或让 Agent session 绑定 `agent.workspace`。记忆 body schema 和 prompt 策略配置在 `session.memory.AgentlyMemory.*` 下。 | V1 每个 Session 只选择一种 memory mode；不提供跨 Workspace user profile 或自动同步。 | `tests/test_cores/test_session.py`、`examples/basic/session_workspace_memory.py`、`docs/*/requests/session-memory.md`。 |
| AgentTask scoped retrieval | Flat 和 TaskBoard 的 scoped retrieval query groups 会降到 Blocks `workspace_operation.search`，执行时使用 `Workspace.retrieve(...)` 召回 records/files，并注入 body-light evidence ledger 与 readback refs。 | AgentTask 在 broad read 前先召回有界 Workspace/file evidence。结构化输出依赖检索事实时，使用 `evidence_use` ids。 | AgentTask retrieval 只是产生证据，不做语义验收；最终声明仍需要 verifier/readback 支撑。 | `tests/test_cores/test_blocks_plugin.py`、`tests/test_agent_task_loop.py`、实验 rounds 007-009。 |
| 公开 typing | 新 Workspace vector export 和 dict-compatible TaskBoard update helpers 都有公开 typing。`TaskBoardGraph.with_cards(...)`、`TaskBoardRevision.next_revision(...)` 同时接受 mapping payload 和 dataclass 值。 | 常见对外更新方法允许用户传 dict-shaped payload；已有强结构时继续用 dataclass。 | 宽类型内部兼容 escape hatch 仍保留为有意设计。 | 全量 pyright 覆盖 `agently/`、`tests/`、`examples`；installed-package pyright smoke。 |

## 兼容性

- Package version: `4.1.3.9`。
- Release manifest: `compatibility/releases/4.1.3.9.json`。
- 推荐 `agently-devtools`: `>=0.1.10,<0.2.0`。
- 下一条 development-line manifest 是 `compatibility/in-development.json`，目标版本
  是 `4.1.4`。

## 验收证据

- Source typing: `python -m pyright --pythonpath "$(python -c 'import sys; print(sys.executable)')" agently tests examples`。
- Full test suite: `python -m pytest -q`。
- 干净 `uv` installed-package smoke：覆盖 Workspace retrieval、`LocalVectorIndex`、
  Session memory 依赖、TaskBoard dict-compatible helpers、LazyImport 缺失依赖诊断和
  installed `py.typed`。
- Companion guidance：`../Agently-Skills` request guidance 已同步 Workspace retrieval
  与 Session memory。

## 延期到 4.1.4

4.1.3.9 不完成 HITL approval / suggestion / guidance / insertion，不完成超出本版
Session/Workspace memory 底座之外的长任务 task-execution memory，也不完成尚未验收的
其他 observation/runtime refinement。
