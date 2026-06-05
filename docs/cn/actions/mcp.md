---
title: MCP
description: 从 Agently agent 调 MCP 服务。
keywords: Agently, MCP, Model Context Protocol, use_mcp, MCPActionExecutor
---

# MCP

> 语言：[English](../../en/actions/mcp.md) · **中文**

MCP（Model Context Protocol）向 AI agent 暴露外部工具。Agently 通过
`MCPActionExecutor` 把 MCP 服务接入 action runtime，所以模型把 MCP tool 与你的
`@agent.action_func` action 看作同一接口。

服务集成优先使用 URL / Streamable HTTP MCP endpoint；本地开发、桌面客户端或单用户本地
server 使用 stdio command config。SSE endpoint 只作为 legacy 兼容路径。

## 最小例子

```python
import os
import asyncio
from dotenv import load_dotenv, find_dotenv
from agently import Agently

load_dotenv(find_dotenv())

Agently.set_settings("OpenAICompatible", {
    "base_url": "${ENV.OPENAI_BASE_URL}",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})

agent = Agently.create_agent()


async def main():
    result = (
        await agent.use_mcp(f"https://mcp.amap.com/mcp?key={os.environ.get('AMAP_API_KEY')}")
        .input("今天上海天气怎么样？")
        .async_start()
    )
    print(result)


asyncio.run(main())
```

`use_mcp(url)` 注册 MCP 服务暴露的所有工具。agent 接着把它们作为 {`@agent.action_func`、`use_tool`、`use_mcp` 工具} 的并集来规划，对模型像同一组。

## API

| 方法 | 行为 |
|---|---|
| `await agent.use_mcp(url)` | 连接服务、列工具、注册；返回 agent 用于链式调用 |
| `await agent.use_mcp(url, headers={...})` | 带自定义 HTTP header（auth token 等） |
| `await agent.use_mcp({"mcpServers": {...}})` | 使用包含一个或多个 HTTP / stdio server 的 MCP config |

默认 executor 会先把 URL + `headers=` 规范化为 MCP config，再交给 FastMCP。

```python
await agent.use_mcp(
    "https://example.com/mcp",
    headers={"Authorization": f"Bearer {token}"},
)
```

本地 stdio server 直接传 MCP config：

```python
await agent.use_mcp({
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "./workspace"],
        }
    }
})
```

## 与自定义 action 混用

```python
@agent.action_func
async def lookup_internal(id: str):
    """在内部数据库查记录。"""
    ...


await agent.use_mcp("https://example-mcp/server")
agent.use_actions(lookup_internal)

# 模型现在在同一 plan 里看到 MCP tool + lookup_internal
result = await agent.input(question).async_start()
```

MCP 提供的 tool 与本地 action 之间没有优先级。模型按名、描述、prompt 上下文来选。

## 看实际调了什么

对于 request-scoped turn，把 turn prompt 传给 action loop 来查看模型实际调了哪些工具：

```python
turn = agent.input("使用 MCP server 回答这个问题。")
records = agent.get_action_result(prompt=turn.prompt)
for r in records:
    print(r)
```

action 记录也写到 `extra.action_logs`（兼容入口下是 `extra.tool_logs`）。

## 常见错误

- **忘 `await`**：`use_mcp(...)` 是 async 因为要从服务列工具。忘 `await` 返回协程，注册悄悄不发生。
- **URL 里传密钥**：优先 header 与环境变量。URL query 参数会进日志。
- **把 MCP 当本地 action 一样用**：hosted MCP 服务可能慢或限速。延迟敏感或高频调用优先本地 action。

## 另见

- [Action Runtime](action-runtime.md) —— `MCPActionExecutor` 是内置 executor 之一
- [工具](tools.md) —— 兼容入口下 `use_mcp(...)` 一样
