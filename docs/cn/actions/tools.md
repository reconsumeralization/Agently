---
title: 工具
description: 兼容工具入口 —— use_tool、use_tools、use_mcp、use_sandbox、tool_func。
keywords: Agently, 工具, use_tool, use_tools, use_mcp, use_sandbox, tool_func
---

# 工具

> 语言：[English](../../en/actions/tools.md) · **中文**

工具系列是 Agently 让模型调函数、MCP 服务、沙箱的**兼容入口**。新代码优先 action 入口 —— 见 [Action Runtime](action-runtime.md)。工具系列仍可用，干净映射到新 runtime，本页给已有它在代码里的用户。

## 入口对照

| 旧（兼容） | 新（推荐） | 做什么 |
|---|---|---|
| `@agent.tool_func` | `@agent.action_func` | 标记函数并推 schema |
| `agent.use_tool(my_func)` | `agent.use_actions(my_func)` | 注册一个 |
| `agent.use_tools([a, b])` | `agent.use_actions([a, b])` | 注册多个 |
| `agent.use_mcp(url)` | `agent.use_mcp(url)` | 不变 —— MCP 挂载 |
| `agent.use_sandbox(...)` | `agent.use_sandbox(...)` | 不变 —— 沙箱挂载 |
| `extra.tool_logs` | `extra.action_logs` | loop 产生的调用记录 |
| `Agently.tool` | `Agently.action` | 全局注册帮手 |

两边都路由进同一个 action runtime。旧名不是独立 `ToolManager` 插件实现 —— 是别名。

## 最小例子

```python
from agently import Agently

agent = Agently.create_agent()


@agent.tool_func
def add(a: int, b: int) -> int:
    """两个整数相加。"""
    return a + b


agent.use_tool(add)

result = agent.input("3333 + 6666 等于多少？").start()
print(result)
```

模型把 `add` 看作可调用工具并决定是否调用。

## auto-func —— 模型驱动的实现

`@agent.auto_func` 装饰器把函数签名 + docstring 变成由模型驱动并使用 agent tool / action 的实现：

```python
@agent.auto_func
def calculate(formula: str) -> int:
    """计算 {formula}。必须用 ACTION 确保答案正确。"""
    ...


print(calculate("3333+6666=?"))
```

被装饰函数无函数体（`...`）。调用时 agent 用注册的 tool 跑模型并返回结果。

## 何时用哪个入口

新项目：用 **action** 入口（见 [Action Runtime](action-runtime.md)）。新功能、插件类型、架构改进都在 action 一侧。

留在 **tool** 入口当：

- 维护已有用旧名的代码。
- 依赖库或旧样例仍使用旧名。

工具系列不会消失 —— 但新功能优先在 action 那侧落地。

## 内置 actions 与 legacy tools

几个常用能力作为内置 action package 发布：

- **Search** —— web 搜索包装
- **Browse** —— 页面抓取与可读正文提取
- **Cmd** —— 低层受限 shell 执行

新代码使用 action-native import path：

```python
from agently.builtins.actions import Browse, Search

agent.use_actions(Search(timeout=15, backend="auto"))
agent.use_actions(Browse())
```

`Search(...)` 注册 `search`、`search_news`、`search_wikipedia` 和
`search_arxiv`。`Browse(...)` 注册 `browse`。实现放在 `agently.builtins.actions`。
`agently.builtins.tools` 只保留为旧代码的薄 legacy import facade；它可以补旧的
`tool_info_list` 元数据，但不应该拥有内置能力实现。`agent.use_tools(...)`、
`agent.tool_func` 和 `Agently.tool` 也仍是受支持的兼容入口。新的内置能力不要再以
`tool_info_list` / `BuiltInTool` 作为 authoring API。

Search 由 `ddgs` package 支撑。默认保留 `backend="auto"`，也可以传入具体 ddgs
backend，例如 `yahoo`、`brave`、`duckduckgo`、`google`、`startpage`、
`mojeek`、`wikipedia` 或 `yandex`。某个 backend 返回 HTTP 200 不代表已经解析到
可用搜索结果；当 backend 没有可用结果时，Search 会继续尝试配置或默认的 ddgs
fallback backends。真实无结果会以成功 action result 返回 `[]`，不会让 action loop
因为“没有结果”而失败。

如果前面的 backend 失败，但后续 fallback backend 返回了可用结果，Action result
会使用 `status="partial_success"`，同时保持 `success=True` 并携带 backend
diagnostics。这表示“有可用证据但存在外部搜索源降级”，不应被 ActionFlow 当作
`action.failed` 终止条件。

Search 和 Browse 都在 package 对象上显式配置 `proxy=` 与 `timeout=`。两者默认也会对
瞬时传输错误重试一次（`max_attempts=2`、`retry_backoff_seconds=0.25`），覆盖
incomplete chunked read、timeout、connection reset、proxy 握手失败这类短暂网络抖动；
它不能替代长期不可用的网络或无法连通的代理。

当 Agent 通过 `agent.language("zh-CN")` 设置语言策略后，注册后的 Search/Browse package
会在未显式配置时接收兼容的 locale 默认值。Search 会把策略作为默认 `region` hint
（例如 `cn-zh`）；Browse 会把策略作为 `Accept-Language` header。这个策略用于查询、
来源召回和过程文本引导，不替代任务自己的来源要求。

`Browse.browse(url)` 直接调用仍保持返回文本的兼容行为，但注册后的 `browse` Action
使用结构化结果。如果所有 Browse backend 都失败，Action result 会是
`status="error"` 并带 backend diagnostics，而不是把 `"Can not browse ..."` 文本当作
成功证据。

注册后的 Browse Action 也负责基础 URL 恢复和远程文件交接。裸域名以及同 host 的
`http` / `https` candidate 会先尝试，再判断整体不可达；结构化结果会包含
selected URL、retry candidates、canonical links、same-site links、attempts，以及
必要的 security downgrade diagnostic。Browse 收到 PDF、Office 文件、图片或其他
download-like binary response，且当前 execution 绑定了 Workspace 时，会把 bytes
物化到 `downloads/`，并返回 file refs 与有界 `read_file` preview。Browse 本身不解析
文档；后续读取由 Workspace file IO handlers 负责。没有绑定 Workspace 时，远程文件
Browse 会 fail closed，不会把 raw bytes 放进模型 hot path。

shell 能力优先使用 `agent.enable_shell(...)`，它挂载托管 `run_bash` action。
`Cmd` 仍作为低层兼容 package 与 Bash 执行实现 helper 保留。

当前 action-native 示例见 `examples/builtin_actions/`。历史 built-in tool 示例已移到
`examples/archived/builtin_tools/`，并在 README 中指向当前替代案例。

## 另见

- [Action Runtime](action-runtime.md) —— 推荐入口，含完整架构
- [MCP](mcp.md) —— `agent.use_mcp(...)` 详细
- [Coding Agents](../development/coding-agents.md) —— 使用内置 search/browse 和自定义 action 的项目如何给 coding agent 提供指引
