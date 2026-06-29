---
title: Agently 4.1.3.8 Release Notes
description: Agently 4.1.3.8 的任务执行策略优化、TaskBoard 策略选择、ACP fallback 能力、输出控制兜底、观测兼容和公开类型元数据说明。
keywords: Agently, release notes, 4.1.3.8, AgentExecution, AgentTaskLoop, TaskBoard, ACP, output control, typing
---

# Agently 4.1.3.8 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.8.md) · **中文**

Agently 4.1.3.8 完成 AgentExecution-backed AgentTaskLoop 路径上的任务执行策略优化。
公开 owner 仍然是 `AgentExecution`；任务执行形态由 AgentExecution / AgentTaskLoop
策略层决定，TaskBoard 只是执行 substrate，ACP 则是既能被直接编排、也能被 recovery
policy 选择的能力。

这不是新的公开 AgentTask lifecycle，也不会把 task-shape analysis 变成硬路由。

## 推荐用法

默认任务执行模式是 `auto`。在 `auto` 中，AgentTaskLoop 会让模型先用自然语言分析任务形态，
再给出很薄的非绑定 execution hint；之后由策略层把有效执行形态解析为 `flat` 或
`taskboard`。用户显式选择优先：

```python
result = (
    agent
    .goal(
        "Prepare a migration risk report.",
        success_criteria=[
            "Cover compatibility, rollout, and rollback risks.",
            "Include evidence for each recommendation.",
        ],
    )
    .effort("medium")
    .strategy("taskboard")
    .output({
        "summary": (str, "short final summary", True),
        "risks": [(str, "one material risk")],
    })
    .get_result()
)

data = result.get_data()
meta = result.get_meta()
effective_shape = meta.get("effective_execution_strategy")
```

嵌套 `AgentExecution` 默认继承父执行的 strategy context，除非用户显式覆盖子执行。

## 核心变动

| 领域 | 变动内容 | 推荐用法 | 兼容性 / 风险 |
|---|---|---|---|
| 执行策略 | `execution_strategy` 默认是 `auto`；策略层解析出 `flat` 或 `taskboard`。`.strategy("flat" | "taskboard")` 重新具备明确执行形态含义。 | 简单任务保持 `auto`；host 明确知道形态时使用 `.strategy(...)`。 | task-shape analysis 只是 evidence 和 hint，不是硬路由。 |
| 策略 verification | Flat 和 TaskBoard 的中间 work unit 都采用 consumer-driven sufficiency。Flat 中非空 `remaining_work` 默认表示下一轮 iteration 应消费这些事实，而不是立刻触发 verifier；`ready_for_final_verification=false` 可用于显式表达同一意图，显式 `true` 则把结果交给终局、阻塞或风险 verification。TaskBoard 下游 card 判断 dependency evidence 是否足够完成自己的目标。终局 verification 失败时，紧凑 `repair_context` 会进入下一轮 Flat work unit 和 Workspace artifact draft 请求。 | 独立 verifier 只放在终局、fan-in/control 合流、可信边界、矛盾或高风险复核点。让下一个 consumer 把 `repair_context` 当作活动修复合同使用，但不把冷侧 provenance 重新放进热 prompt。 | 去掉每个行动后的冗余 verifier 请求，但不取消最终验收、终局修复反馈、可信 Workspace/source/readback guard。 |
| TaskBoard 路径 | TaskBoard 不再做复杂度分类；只有策略层选择它之后才执行，并保留 save/load/resume、handler diagnostics、card evidence contracts 和 consumer-driven continuation。 | 把 TaskBoard 当作分支或多视角任务的执行 substrate；由真正消费上游证据的下游 card 判断信息是否足够完成自己的目标。 | TaskBoard 是 board/dependency/patch 协调器，不是单独的执行载体。 |
| effort 反思密度 | `effort("low" | "medium" | "high")` 映射 reflection density。low 保留 final reflection 和 planner 标记的重要过程点；medium 在大节点或 card/tick 边界反思；high 在每个可观测 Action、ACP call、card、bounded step 和 final point 反思。 | 选择能提供足够审计证据的最低 effort。 | reflection 进入 Workspace evidence、replan 和 verifier 输入，但不能单独算完成证据。 |
| Workspace artifact readback | AgentTask 交付的 artifact 只有在 Workspace 可信读回把 `path`、`bytes`、`sha256`、有界 preview 和 `file_refs` 记录到冷证据后才能被接受；模型热 verifier 输入只看 path/ref handle、有界内容或 preview、截断状态，并在长 artifact 需要检查必需 section、source/risk/reference/coverage 锚点时看到有界 `targeted_readbacks`；完整性 metadata 仍可由程序溯源。`capability_evidence.artifacts.readback` 使用路径 handle，不使用 `path#sha` id。 | 用 `artifact_markdown` 或 `artifact_manifest.sections` 生成交付物，让 Workspace 产出证据链。 | 写入成功但读回失败或不足时报告 `agent_task.workspace_artifact.readback_failed` / `agent_task.workspace_artifact.readback_insufficient`，不是泛化成预算或迭代失败。 |
| TaskBoard 冷读回 | TaskBoard readback card 可以通过有界冷读回检查 Action artifact refs 和可信 Workspace file/content refs。框架生成的 readback card 会把 evidence scope 扩展到直接依赖和上游 evidence card，continuation card 不再针对同一个未解决证据缺口递归合成新的 readback 链。结构化 `target_refs` 会按 ref 类型分流：HTTP/HTTPS 外部 ref 变成 Action evidence 工作；Workspace/content 路径和 retained-note ref 变成直接的有界 Workspace readback card。 | scoped cold evidence inspection 使用 readback card；证据仍不足时提出其他可执行工作，而不是重复同一 readback。 | 保持默认不做硬资源卡控，同时避免纯 readback 循环，并避免模型自由改写 Workspace 读回事实。 |
| Scoped Workspace retrieval | Flat 和 TaskBoard work unit 都可以携带 `scoped_retrieval.query_groups`；共享 BlockCarrier 会把 query groups 降到前置 Blocks `workspace_operation.search` 事实，并把紧凑的模型热视图 `scoped_retrieval_results` 注入有界 `agent_step` 或 card，完整 SHA/字节/backend provenance 留在原始 Workspace/Blocks 证据中。TaskBoard 的 Workspace-operation prompt 视图、available readback handle、readback work-unit 热 payload、Action artifact readback preview 和中间 Workspace readback preview 都使用同一 hot/cold 拆分。query group 可以选择 `workspace_index`、`workspace_files` 或 `workspace_index_and_files`；record collection 应放在 `filters.collection`，精确 record kind 可用 `filters.kind`，文件 scope 使用 `path`/`pattern`。Workspace file search 接受递归 `pattern="**"`，并在可用时使用 `rg` 作为 grep-style 搜索引擎。`evidence_snippet` 事实会暴露有界上下文是否 `truncated`。 | 当 scoped Workspace/file evidence 能减少 prompt 输入时，先 search，再让下游模型判断 snippet 是否有用或是否需要继续 readback。TaskBoard scoped-retrieval card 返回 blocked/insufficient 且没有显式 next action 时，会合成放宽检索的 evidence card 和 continuation card，而不是依赖终局 verifier 修补中间证据。 | 搜索命中不是本地语义验收、质量 gate 或完成证据。Flat 和 TaskBoard 文件/grep retrieval 都已有成对 hot-context focused 对照证据；TaskBoard SQLite/FTS continuation 已在框架契约层实现，完整效果 claim 仍需复跑实验。 |
| ACP 能力 | ACP 是 Action 加 `ExecutionResource(kind="acp")`；可以被 planner/user 直接选择，也可以在 retry 耗尽后由 recovery 使用。 | 只有需要 ACP 时才调用 `.use_acp(...)`；`acp_list_agents` 会给出 `codex`、`claude code` / `cc`、`openclaw`、`hermes` / `hermes agent`、`gemini` 等常见 adapter 名称提示。 | ACP 不绕过 AgentExecution 或 AgentTaskLoop 策略，adapter hint 也不是 runnable-agent evidence。 |
| 可选依赖加载 | MCP 和 ACP 都使用 `utils.LazyImport`；没有显式 `.use_mcp(...)` 或 `.use_acp(...)` 时不会加载可选包。 | 普通 agent 保持轻依赖；在能力边界显式启用可选 runtime。 | 可选依赖缺失只在相关路径被使用时通过 LazyImport 诊断暴露。 |
| 强格式过程输出 | 强格式中间模型请求使用 Agently `.output(..., format=...)` 和恰当 parser。声明的非 JSON parser 失败时可切回 JSON，且只接受能解析成 dict 的值，并携带诊断。 | 过程契约使用 `.output(...)`，不要用关键词或本地 scorecard 替代语义判断。 | 兜底是解析恢复路径，不是语义捷径。 |
| Delta 文本流 | `get_async_generator(type="delta")` 仍是公开文本增量流。复杂 AgentTask / AgentExecution 会把模板 progress、snapshot、heartbeat 状态、phase 状态、retry marker 和任务终态结果投影成段落文本，同时 `instant` 保留结构化载荷。 | 面向用户的流式文本用 `delta`；结构化 UI 状态、诊断或 DevTools 式回放用 `instant` 或结构化 execution items。持久化 artifact writer 应优先消费结构化 `$status`；如果明确选择纯文本流，则必须在消费侧把 `<$retry>...</$retry>` 当作 replay boundary 处理，而不是为了拿 instant 字段把自由正文强行塞进 `.output()`。 | 既有文本增量仍按字符串输出；过程事件投影是增量行为。 |
| 观测兼容 | AgentExecution 会把 flat 和 TaskBoard 过程 stream item 投影为 `agent_execution.stream` RuntimeEvent；task/TaskBoard/ACP/reflection payload 保持通用、fail-open。`agent_task.action.*`、`model.status` 和 `model_request_telemetry` 仍是 observation-only；终态 `model.status` 可携带输入/输出字符长度估算且不暴露 raw request payload。 | 使用 `agently-devtools >=0.1.10,<0.2.0`；DevTools 展示 AgentTask action observations，并展示单次 model request usage 与当前选中分支 descendant 聚合 usage，provider token 不可得时显示 `NaN`，输入/输出长度估算只作诊断。 | DevTools 可以 ingest、store、query 和 replay AgentExecution、flat、TaskBoard、action 与 usage observation facts，但不拥有任务策略、预算、retry、质量或完成验收语义。 |
| 公开类型 | 包内新增 `agently/py.typed`，并为常用公开 facade 方法补齐类型。 | IDE 和 pyright-compatible 工具可以直接读取安装后的 Agently 类型。 | 少数宽类型内部表面仍作为兼容 escape hatch 保留。 |

## 兼容性

- Package version: `4.1.3.8`。
- Release manifest: `compatibility/releases/4.1.3.8.json`。
- 推荐 `agently-devtools`: `>=0.1.10,<0.2.0`。
- Development-line planning 仍保留在 `compatibility/in-development.json`，直到下一条 release line 移动。

## 延期范围

4.1.3.8 不完成 multi-task scheduling、background autonomous scheduling、
production distributed task recovery、production Redis/Postgres 或 object-storage
Workspace providers，也不完成 AgentTaskLoop 的 TriggerFlow-backed AdaptiveLoop /
BootstrapLoop packaging。
