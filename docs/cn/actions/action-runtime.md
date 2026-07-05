---
title: Action Runtime
description: TriggerFlow 之下的 action 架构 —— ActionRuntime、ActionFlow、ActionExecutor 与 Agently.action 入口。
keywords: Agently, action runtime, ActionRuntime, ActionFlow, ActionExecutor, action_func, use_actions
---

# Action Runtime

> 语言：[English](../../en/actions/action-runtime.md) · **中文**

Agently 的 action 栈在编排层之下有三个可替换插件层：

```text
   TriggerFlow                  ◄── action 之上的编排（loop、分支、pause/resume）
       │
       ▼
   ActionRuntime                ◄── 规划 + 派发
       │  （ActionFlow 是与编排层的桥）
       ▼
   ActionExecutor               ◄── 原子执行（local function、MCP、sandbox）
```

## 各层详细

| 层 | 拥有什么 | 默认 builtin |
|---|---|---|
| `TriggerFlow` | action 之上的高层编排（loop、分支、pause/resume、子流）—— 见 [TriggerFlow](../triggerflow/overview.md) | `TriggerFlow` 核心 |
| `ActionRuntime` | 规划协议、action 调用归一化、默认执行编排 | `AgentlyActionRuntime` |
| `ActionFlow` | `ActionRuntime` 与 flow 表示之间的桥 | `TriggerFlowActionFlow` |
| `ActionExecutor` | 单个 action 实际怎么跑 | local function、MCP、Python/Bash sandbox、Search/Browse、Node.js、Docker、SQLite executors |
| `ExecutionResource` | executor 调用前需要准备的托管执行依赖 | MCP、Bash、Python、Node、Docker、Browser、SQLite providers |

`Action` 是 `agently.core` 根导出的执行门面，连线：

- `ActionRegistry` 与 `ActionDispatcher`（稳定核心原语）
- 一个 active `ActionRuntime` 插件
- 一个 active `ActionFlow` 插件

默认连线：

```text
Agent → ActionExtension → Action 门面 → ActionRuntime → ActionFlow → ActionExecutor
```

## 插件类型

可替换的插件类型：

- `ActionRuntime` —— 改规划协议或调用归一化
- `ActionFlow` —— 改编排形态（如自定义 flow 表示）
- `ActionExecutor` —— 加新后端（HTTP、gRPC、自定义沙箱、远程 worker）

从 `agently.types.plugins` import 协议与 handler 别名：

```python
from agently.types.plugins import (
    ActionExecutor,
    ActionRuntime,
    ActionFlow,
    ActionPlanningHandler,
    ActionExecutionHandler,
)
```

> 旧的 `ToolManager` 插件类型与 `AgentlyToolManager` 类仅作为遗留兼容保留；deprecation warning 按 deprecated API 在每个 Python 进程内只发一次，除非关闭了 `runtime.show_deprecation_warnings`。新插件不要写在 `ToolManager` 上。

## 推荐入口 —— actions

新代码：

```python
from agently import Agently

agent = Agently.create_agent()


@agent.action_func
async def add(a: int, b: int) -> int:
    """两个整数相加。"""
    return a + b


@agent.action_func
async def python_code_executor(python_code: str):
    """执行 Python 代码并返回结果。"""
    ...


agent.use_actions([add, python_code_executor])

# 或一次性注册并执行
@agent.auto_func
def calculate(formula: str) -> int:
    """计算 {formula}。使用可用的 action。"""
    ...

print(calculate("3333+6666=?"))
```

| 入口 | 用途 |
|---|---|
| `@agent.action_func` | 标记函数为 action，从签名 + docstring 推 schema |
| `agent.use_actions(actions)` | 在 agent 上注册 list、单个 action 或字符串名 action |
| `agent.use_actions(["name1", "name2"])` | 按名注册预注册的 action |
| `agent.use_actions(Search(...))` | 挂载来自 `agently.builtins.actions` 的内置 Search package |
| `agent.use_actions(Browse(...))` | 挂载来自 `agently.builtins.actions` 的内置 Browse package |
| `agent.enable_python(...)` | 挂载托管 `run_python` action，用于确定性代码执行 |
| `agent.enable_shell(...)` | 挂载带 workspace root、命令 allowlist、timeout 和有界输出预览的托管 `run_bash` action |
| `agent.enable_nodejs(...)` | 挂载托管 `run_nodejs` action |
| `agent.enable_sqlite(...)` | 挂载托管 `query_sqlite` action |
| `agent.enable_workspace_file_actions(...)` | 把当前 Workspace 文件作业区暴露成 handler-backed 列表、搜索、读取、写入 actions；`export=True` 且 `write=True` 时额外暴露 `export_file` |
| `agent.enable_coding_agent_actions(...)` | 暴露 coding-agent Workspace actions，用于文件读回、glob/grep 检索、定点编辑、unified diff patch 和带 guard 的整文件写入 |
| `@agent.auto_func` | 把 Python 函数签名 + docstring 变成模型驱动的实现，使用 agent 的 action |
| `agent.get_action_result(prompt=turn.prompt)` | 取 request-scoped turn 的 action 调用记录 |
| `extra.action_logs` | action loop 期间产生的结构化日志 |

`agent.action.get_action_info()` 和 `agent.action.get_tool_info()` 默认返回该
agent 上可见的 action/tool schema，包括 agent-scoped actions、通过
`agent.use_mcp(...)` 挂载的 MCP tools，以及 `enable_*` component helpers。只有需要
窄范围子集时才传显式 `tags=[...]`。托管执行环境 metadata 在这个可见 schema
里会脱敏原始 `env` 值，但保留 env key；provider 只会在实际执行路径中拿到 raw env。

应用代码要给模型开放 Python、shell、workspace 等常见能力时，优先使用
`enable_*` helpers。只有在开发自定义 Action 后端时，才需要使用
`register_action(..., executor=..., execution_resources=[...])`。

coding-agent 风格的本地文件工作优先使用
`agent.enable_coding_agent_actions(...)`。它会在当前 Workspace file root 上暴露
`read_file`、`glob_files`、`grep_files`、`edit_file`、`apply_patch` 和带 guard 的
`write_file`。`edit_file(...)` 可使用 `expected_sha256` stale guard；
`apply_patch(...)` 应用 unified diff，并可要求精确的 `expected_files`；
coding-agent mode 下的 `write_file(...)` 默认要求先读过目标文件或提供 expected hash，
除非 host 显式关闭这个 guard。测试、构建、git inspection 和只读诊断使用 shell；
文件读取、检索、编辑和写入使用 Workspace file actions。

`agent.enable_shell(...)` 不传显式 `commands=...` allowlist 时，Agently 会使用小型
safe shell profile，例如 `pwd`、`ls`、`rg`、`cat`、`git status`、`git diff`、
`git log`、`python -m pytest` 和 `python -m pyright`。stdout/stderr 以有界 preview
返回；某个 stream 超过 `max_output_chars` 时，完整 stream 会写入 Workspace root 下的
`artifacts/shell/`，并在 action result 中返回引用。
`allow_unsafe` 是 host-only 的直接执行授权，不会出现在模型可见的 shell action schema
中；模型计划出的 action input 里即使包含该字段也会被清洗。模型选择的命令超出 safe
profile 时，应通过需要审批的 action 或 ExecutionExchange provider 路由，而不是允许
模型输出自行授予 bypass。
自定义 action 如果需要仅 host/direct call 可用的参数，可以用
`meta={"host_only_input_keys": [...]}` 声明；Action Runtime 会从模型计划出的
`structured_plan` 和 native tool-call 输入里清洗这些 key，同时保留 host/direct call。

内置能力 package 位于 `agently.builtins.actions`。例如：

```python
from agently.builtins.actions import Browse, Search

agent.use_actions(Search(timeout=15, backend="auto"))
agent.use_actions(Browse())
```

Search 是 Action-native package，不进入 ExecutionResource；proxy、timeout、
backend、region 都属于 package/executor 配置。Browse 也是 Action-native；默认主线是
Playwright -> restricted curl -> BS4，pyautogui 保留为 legacy/advanced 配置。curl backend
是 Browse 内部的 URL fetch fallback，不是暴露给模型的 shell access。如果 Browse action
需要托管 browser/page/session，可以启用 Browser ExecutionResource provider。

Agent Client Protocol（ACP）coding agent 作为 Action capability 暴露，不是
AgentExecution route。使用 `agent.use_acp(on_missing="skip")` 可以扫描本地
ACP endpoint 和内置本地 coding-agent CLI adapter，并且只在存在已验证可运行 agent 时注册
`acp_list_agents` 和 `acp_run_task`。`acp_list_agents` 还会返回非绑定的常见
ACP adapter 名称提示，例如 `codex`、`claude code` / `cc`、`openclaw`、
`hermes` / `hermes agent` 和 `gemini`；这些提示不会让 agent 变成 runnable。
内置本地 CLI adapter 会检查常见 Codex 和 Claude Code 命令位置以及当前进程 `PATH`，
使用框架固定 argv 模板，不暴露模型可见 shell execution。
默认 `on_missing="skip"` 只记录 diagnostics，不会伪造 runnable agent；
`on_missing="error"` 会 fail closed。ACP run action 会声明
`ExecutionResource(kind="acp")`，让 root scope 和 lifecycle 事实留在 resource 层。
如果省略 `root`，`agent.use_acp()` 使用当前 Agent 绑定的 Workspace `files_root`
作为 coding-agent project root；只有 host 明确授权另一个项目目录时才传入
`root=...`。ACP session 复用是 AgentExecution 内部 resource policy，不是普通任务启动
选项。CLI adapter 会标记 `acp_session.persistence="stateless_cli"`，除非存在真实可恢复的
ACP protocol session。

AgentTask 也可以在 bounded step 或 TaskBoard card 执行失败、且配置的重试耗尽后，
把 ACP 作为显式启用的 recovery fallback。这个路径仍然调用已注册的 `acp_run_task`
Action，并使用 `ExecutionResource(kind="acp")`；ACP 不是绕过 AgentExecution 或 task
strategy policy 的新 route。如果 host 从未调用 `agent.use_acp(...)`，fallback 只会记录
skipped diagnostics，不会导入 ACP 依赖，也不会伪造可用 agent。

`enable_*` helpers 的 `desc=` 是可选项。默认会作为补充说明追加，确保模型仍然看到基础用法和安全边界。
如果你确实要替换默认描述，使用 `desc_mode="override"`；如果要忽略传入描述、只保留内置描述，使用
`desc_mode="default"`。

## 模型来源输入安全

模型规划产生的 Action command 在 Action 边界被视为不可信输入。对于
`structured_plan` 和 `native_tool_calls` command，`ActionDispatcher` 会在调用
executor 之前，把 `action_input` 过滤到注册时 `ActionSpec.kwargs` 声明过的 key。
host 的 `direct` / `dry_run` 调用保持既有行为，不做这类过滤。

被过滤的调用会在 `ActionResult` 上保留结构化 diagnostics，包括
`action.input.unexpected_keys_stripped`、被移除的 key，以及原始 kwargs 和实际执行 kwargs
的有界预览。timeout 和 executor exception 也会返回带 diagnostics 的结构化 Action failed
结果。RuntimeEvent 消费者可以观察这些事实，但 RuntimeEvent 不负责输入过滤或授权。

## 执行回溯

`run_bash`、`run_python`、`run_nodejs`、`query_sqlite`、`browse`、`search`
这类指令型 action 会记录一份执行 digest 和一组 artifact references，用来控制后续模型上下文长度。

后续 action planning round 默认看到的是 digest。它包含 action id、call id、目的、状态、精简指令预览、
结果预览、preview 截断元数据、脱敏说明、artifact refs，以及 Action 返回的 Workspace file refs。
完整代码、shell 输出、SQL 结果集、页面 HTML、截图、日志等原始内容会以脱敏 artifact 形式保留，
不会默认塞进每一轮 prompt。artifact refs 会包含 role、media type、size/bytes、preview size、
SHA-256 和截断标记，消费方可以明确知道 preview 不是完整证据。
显式返回 `artifacts` 或 `artifact_refs` 的 action 即使输出很小也使用同一合同。
这包括 `MCPActionExecutor` 暴露的 MCP resource/content block；Agently 记录
声明过的 artifact metadata，但不会通过扫描目录推断未声明的文件写入。
如果宿主 action 会生成供 AgentTask 或 TaskBoard 后续消费的文件，建议返回带
path、size/bytes、media type，以及可用时 SHA-256 的 typed `file_refs` 或
`artifact_refs`。只有 `{filename, path, size}` 这类 path-only payload 时，Agently
会把它保留为有界 Action result evidence 和 ref pointer；只有当 path 位于
Workspace files root 内且 Workspace readback 成功时，才会升级为可信 Workspace
file ref。
Search、Browse 等内置 Web actions 在运行时不会弹出包安装确认。可选依赖缺失会
作为结构化 Action failure 暴露给宿主，由宿主决定安装、重试或降级。
如果 digest 对后续规划或回复 hot path 仍然过大，Agently 会再次压缩模型可见 digest：
`result` 保留有界 digest，重复的 `data` / `model_digest` 字段可能变成
`same_as="result"` 指针，artifact refs 会省略 preview 正文但保留 readback id。
这个压缩只作用于 hot-path 模型上下文；完整脱敏内容仍留在 Action artifact store 里，
需要显式读回。

如果模型或应用需要回溯细节，可以显式读取：

```python
turn = agent.input("使用 action，并总结执行结果。")
records = agent.get_action_result(prompt=turn.prompt)
artifact_ref = records[0]["artifact_refs"][0]

raw = agent.action.read_action_artifact(
    artifact_id=artifact_ref["artifact_id"],
    action_call_id=artifact_ref["action_call_id"],
)
```

`Action.to_action_results(records)` 对指令型 action 使用 digest，因此后续回复能知道发生了什么，
但不会默认拿到完整 payload。

`max_output_bytes` 是输出证据策略，不是破坏性存储操作。Action 输出超过该限制时，Agently 会记录
diagnostics，并把完整值保留在 artifact ref 后面；模型可见路径仍然使用有界 preview。

当 host 显式调用 `agent.get_action_result(prompt=...)` 时，即使返回的
records 为空，Agently 也会把该 prompt 标记为已经消费过 action loop。之后同一
prompt 的响应读取不会为了生成最终文本再次进入 ActionRuntime。

host 需要权威 action evidence 摘要时，可以使用
`agent.action.summarize_records(records)`：

```python
summary = agent.action.summarize_records(
    records,
    validation_command_markers=["pytest", "pyright"],
)

assert summary["latest_validation"]["status"] in {"passed", "failed"}
```

摘要会报告 failed actions、尝试过的命令、成功命令，以及最后一个匹配的验证命令。

## 兼容入口 —— tools

旧入口仍可用：

```python
@agent.tool_func
def add(a: int, b: int) -> int:
    return a + b

agent.use_tool(add)
agent.use_tools([add])
agent.use_mcp("https://...")
agent.use_sandbox(...)
extra.tool_logs  # 旧入口的 extra.action_logs
```

它们仍是有效的公开挂载入口。内部映射到新 action runtime —— 不意味着 `ToolManager` 实现。方便时迁到 action 入口；什么都不会立刻坏。

## 规划模型 key

Action planning 是模型拥有的步骤。如果 Agent 使用 `model_pool`，应把
`action.planning_model_key` 设置为负责规划 action round 的业务模型 key：

```python
agent.set_settings("model_pool", {"task-main": "deepseek-chat-prod"})
agent.set_settings("model_profiles", {
    "deepseek-chat-prod": {
        "provider": "OpenAICompatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_pool": "deepseek-prod",
    }
})
agent.set_settings("action.planning_model_key", "task-main")
```

这个配置同时作用于默认 structured-plan 和 native tool-call planning
路径。当 SkillsExecutor 或 AgentTask 把一个 bounded action round
委托给 ActionRuntime 时尤其重要，否则 action planning 可能没有显式使用
预期的 `model_pool` 业务 key。

`agent.get_action_result(..., timeout=N)` 会约束完整 action loop，包括
structured planning 和 native tool-call selection。如果 loop 不能在 deadline
前结束，Agently 会抛出 `RuntimeStageStallError`，其中
`stage="action_loop_close"`。

当 `planning_protocol="native_tool_calls"` 没有拿到 provider-native tool calls
时，Agently 会返回一个 `skipped` 诊断 action record，诊断 code 为
`action_runtime.native_tool_calls.empty`。host 应把它当作 planning evidence，
而不是已执行工作。

当连续 action rounds 反复选择同一批失败 action id，并且这些记录都没有产生进展时，
默认的 `TriggerFlowActionFlow` 会关闭当前 bounded action step，返回已经获得的失败
evidence。默认阈值是 `action.loop.max_consecutive_failed_rounds_per_action = 2`
（`tool.loop...` 仍作为兼容别名）。这不是任务预算；上层 owner，例如
AgentTask，可以基于这些结构化失败记录继续 verify、replan 或 block。

## handler 接口

如果你写自定义 `ActionRuntime` 或 `ActionFlow` 插件，规划与执行 handler 用稳定的双参数契约：

```python
async def planning_handler(
    context: ActionRunContext,
    request: ActionPlanningRequest,
) -> ActionDecision:
    ...


async def execution_handler(
    context: ActionRunContext,
    request: ActionExecutionRequest,
) -> list[ActionResult]:
    ...
```

context 字段含 `prompt`、`settings`、`agent_name`、`round_index`、`max_rounds`、`done_plans`、`last_round_records`、`action`、`runtime`。request 字段含 `action_list`、`planning_protocol`、`action_calls`、`async_call_action`、`concurrency`、`timeout`。

自定义 `ActionFlow` 插件可以接受可选的 `runtime_observation_handler`
关键字参数。若提供，flow 应把普通 observation dict 交给这个 handler，
不要直接发送官方 `action.*` 或 `tool.*` RuntimeEvent；core 会把这些
observation 映射到官方事件流。

没有 legacy positional 签名 —— 公开契约只是 `(context, request)`。

## 扩展指南

| 你想改 | 替换 |
|---|---|
| 仅后端（HTTP、gRPC、远程 worker、sandbox） | `ActionExecutor` |
| 规划协议或调用归一化 | `ActionRuntime` |
| runtime 与 flow 之间的编排形态 | `ActionFlow` |
| 多个 action 调用之上的更高层流控 | 用 `TriggerFlow` 在 runtime 之上 —— 不要把它塞进 executor |
| MCP/sandbox/process 类依赖的生命周期 | 声明 `ExecutionResource` requirement —— 不要把生命周期藏进 executor |

## 另见

- [Actions 概览](overview.md) —— Action Runtime 到哪里停止、编排从哪里开始
- [ExecutionResource](execution-environment.md) —— 托管 MCP/sandbox 执行依赖
- [工具](tools.md) —— 兼容入口详细
- [MCP](mcp.md) —— `agent.use_mcp(...)`
- [TriggerFlow 概览](../triggerflow/overview.md) —— action 之上的编排
