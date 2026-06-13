---
title: Execution Resource
description: Action 与 TriggerFlow 的托管执行资源。
keywords: Agently, ExecutionResource, Action, TriggerFlow, sandbox, MCP, runtime_resources
---

# Execution Resource

> 语言：[English](../../en/actions/execution-environment.md) · **中文**

> 在 4.1.3.8 的 Workspace/ActionRuntime 边界重构中更名：托管的活动资源接缝现在
> 称为 **ExecutionResource**（`ExecutionResourceManager`、
> `ExecutionResourceProvider`、`Agently.execution_resource`），旧的
> `ExecutionEnvironment*` 名称已移除。本页保留原 URL 以保持链接稳定。

Execution Resource 是框架级执行资源层，用来在 action 或 workflow step
真正执行前准备、复用和释放托管执行资源。

它负责 MCP transport、命令 runner、sandbox、browser、SQLite connection 和外部进程
runner 等资源的生命周期和 policy。Action 与 TriggerFlow 可以声明需要这些环境，但不拥有环境生命周期。

## 面向对象

多数应用开发者不应该从这里开始。优先使用描述意图的 built-in actions 和
Agent Component helpers，例如启用 Python、shell、workspace、MCP、SQLite、
vector store 或 coding workspace 能力。

在这些情况下阅读本页：

- 你在写依赖托管 live resource 的自定义 `ActionExecutor`
- 你在写 `ExecutionResourceProvider` 插件
- 你在 review Action 或 TriggerFlow 如何接收托管资源
- 你在设计需要 sandbox、process、MCP、client、credential 或 cleanup 生命周期的新 built-in capability

不要把 `Agently.execution_resource` 暴露成默认应用开发心智。它是更高层能力背后的 core lifecycle 层。

## 所在位置

```text
Agent Component / built-in Action / custom Action / TriggerFlow / Skills plan
        |
        v
ActionSpec.execution_resources or TriggerFlow execution requirements
        |
        v
ExecutionResourceManager
        |
        v
ExecutionResourceProvider
        |
        v
managed handle / live resource
```

V1 全局 manager 暴露为：

```python
from agently import Agently

Agently.execution_resource
```

多数业务代码不需要直接调用 manager。内置 MCP、Bash、Python、Node.js、Docker、
Browser、SQLite action 可以声明自己的 requirement，Action dispatcher 在 executor
调用前自动 ensure。

更完整的 ownership 模型见
[Architecture / 扩展边界](../architecture/extension-boundaries.md)。

## 内置行为

内置 provider：

| Kind | 使用方 | 托管资源 |
|---|---|---|
| `mcp` | `agent.use_mcp(...)` / MCP actions | MCP transport resource |
| `bash` | `agent.enable_shell(...)` / Bash sandbox actions | 配置后的命令 runner |
| `python` | `agent.enable_python(...)` / Python sandbox actions | 配置后的 Python sandbox |
| `node` | `agent.enable_nodejs(...)` / Node.js executor actions | 配置后的 Node.js runner |
| `docker` | Docker executor actions | Docker CLI runner |
| `browser` | 选择托管 browser resource 的 Browse actions | 托管 browser/page/session wrapper |
| `sqlite` | `agent.enable_sqlite(...)` / SQLite executor actions | SQLite connection |

Search 故意不放在这里。它是无状态的 Action-native capability package；proxy、timeout、
backend、region 属于 Search package/executor 配置，不属于 Execution Environment。

这些 provider 是低层环境实现。面向用户的能力通常应该暴露为 Action，场景快捷入口应该通过 Agent Component 或未来的 `agent.enable_*` helpers 暴露。

Action 执行流：

```text
ActionCall
  -> resolve ActionSpec
  -> ensure ActionSpec.execution_resources
  -> 把 execution_resource_resources 注入 action_call
  -> ActionExecutor.execute(...)
  -> 释放 action_call scope 的 handles
```

自定义 `ActionExecutor.execute(...)` 签名不变。托管 handle 会通过
`action_call["execution_resource_handles"]` 传入，live resource 会通过
`action_call["execution_resource_resources"]` 传入。

## TriggerFlow

TriggerFlow 仍然使用 `runtime_resources` 作为 execution-local live resource 的兼容入口。
Execution Environment 不重命名也不替代这个 API。

可以在创建或启动 execution 时传入托管 requirement：

```python
execution = flow.create_execution(
    execution_resources=[
        {
            "kind": "python",
            "scope": "execution",
            "resource_key": "sandbox",
        }
    ],
)
```

manager 会 ensure 资源，把它注入 execution-local resources，并在 execution close 时释放。
手动传入的 `runtime_resources={...}` 仍是 unmanaged，不参与 manager 的 health check
或自动释放。

## 直接 Manager API

这组 API 面向框架、action 和 plugin 开发者。

manager 支持：

```python
Agently.execution_resource.declare(requirement)
Agently.execution_resource.ensure(requirement_or_id)
await Agently.execution_resource.async_ensure(requirement_or_id)
Agently.execution_resource.release(handle_or_id)
Agently.execution_resource.release_scope("session", owner_id)
Agently.execution_resource.inspect(id)
Agently.execution_resource.list(scope="execution")
Agently.policy_approval.register_handler("my_handler", handler)
Agently.configure_policy_approval(handler="my_handler")
```

声明是 lazy 的：只校验和记录 requirement，不启动任何东西。`ensure(...)` 会在 policy
与 approval 允许的情况下启动或复用 handle。approval 由框架全局
`Agently.policy_approval` handler 决定。默认 `input_timeout_fail` 只会在交互式 CLI
中提示输入，并在超时后失败；非交互服务环境会立即失败。包裹 TriggerFlow execution
的服务应注册自己的 handler，例如写入 pending approval 后用 `continue_with(...)` 恢复。
复用 ready handle 前，manager 会调用
`provider.async_health_check(handle)`。健康则 `ref_count + 1` 后复用；不健康则发出
`execution_resource.unhealthy`，释放旧 handle，再 ensure 一个新 handle。V2 不加入后台
health scheduler、lease TTL 或自动 reconnect loop。

如果你在开发应用，应该先检查是否已有 built-in action 或 Agent Component 暴露了你需要的能力。

## Observation

manager 发出 `execution_resource.*` 事件：

- `execution_resource.declared`
- `execution_resource.approval_required`
- `execution_resource.ensuring`
- `execution_resource.ready`
- `execution_resource.unhealthy`
- `execution_resource.releasing`
- `execution_resource.released`
- `execution_resource.failed`

payload 只包含稳定 id 与状态元信息，不能包含原始凭证、环境变量、命令 secret 或 live resource 对象。

## Examples

可运行示例见
[`examples/execution_resource`](../../../examples/execution_resource/README.md)。
建议先看本地 `agent.enable_python(...)` quickstart，再看 Ollama 和 DeepSeek
驱动的模型决策示例。TriggerFlow 示例面向需要托管 execution-local resource 的
workflow 或框架开发者。

## 另见

- [Action Runtime](action-runtime.md)
- [MCP](mcp.md)
- [TriggerFlow State 与 Resources](../triggerflow/state-and-resources.md)
