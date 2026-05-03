---
title: MCP Integration
description: "Agently MCP integration docs: fastmcp>=3 requirements, HTTP/local transports, and troubleshooting."
keywords: "Agently,MCP,fastmcp,tool calling,agent.use_mcp"
---

# MCP Integration

> Applies to: 4.0.8.1+

Agently can ingest tools from an MCP server and expose them to Agent for on-demand tool calling.

## 1. Version requirement

As of `v4.0.8.1`, MCP integration requires:

- `fastmcp >= 3`

If you still use an older fastmcp release, upgrade first.

## 2. Basic onboarding (HTTP MCP)

```python
import asyncio
from agently import Agently

agent = Agently.create_agent()

async def main():
    await agent.use_mcp("http://localhost:8080/mcp")
    result = await agent.input("Use tools to compute 333 + 546").async_start()
    print(result)

asyncio.run(main())
```

`use_mcp(...)` will:

1. list tools from MCP server
2. convert schemas into Agently tool format
3. register them into current agent scope

## 3. Local script transport (stdio)

```python
import asyncio
from pathlib import Path
from agently import Agently

agent = Agently.create_agent()

async def main():
    mcp_script = Path("./cal_mcp_server.py").resolve()
    await agent.use_mcp(str(mcp_script))
    result = await agent.input("Use tools to compute 21 * 34").async_start()
    print(result)

asyncio.run(main())
```

## 4. Relationship with tool system

After onboarding, MCP tools behave like regular registered tools:

- participate in auto tool decisions
- emit call traces in `extra.tool_logs`
- can be observed with `runtime.show_tool_logs`

## 5. Common issues

## 5.1 Import error for fastmcp

- ensure `fastmcp>=3` is installed
- verify your runtime env matches install env (venv/poetry)

## 5.2 Tools are onboarded but model does not call them

Recommendations:

- improve tool descriptions/kwargs metadata
- explicitly instruct model to use tools when needed
- validate chain with simple prompts first

## 5.3 Tool call fails but response still returns

MCP failures may surface as structured tool errors (for example `{"error": ...}`).
Handle these explicitly in downstream output logic.
