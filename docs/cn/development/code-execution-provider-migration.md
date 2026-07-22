---
title: Code Execution Provider 迁移
description: provider-neutral、Workspace-backed 代码执行契约，以及外部隔离 provider 的迁移检查表。
keywords: Agently, code execution, ExecutionResourceProvider, TaskWorkspace, provider migration
---

# Code Execution Provider 迁移

> 语言：[English](../../en/development/code-execution-provider-migration.md) · **中文**

本文面向 provider 贡献者。Agently 基础实现负责 provider-neutral 执行契约；具体隔离
实现仍由贡献者在自己的 PR 中持有、重构、测试和接受 review。

## 固定所有权与调用顺序

`code_execution` 是资源 kind。Docker、container runtime 变体、host policy、远程
worker 和显式无防护本地 runner 都是 provider 实现，不是新的资源 kind。

每次调用固定为：

```text
TaskWorkspace
  -> 签发 TaskWorkspaceAccessGrant
  -> 选择并绑定 ExecutionResourceProvider
  -> 落地不可变 CodeExecutionBundle
  -> 执行 adapter 持有的 argv plan
  -> 把声明的输出收集回 TaskWorkspace
  -> 释放 provider
  -> 关闭 grant
```

provider 只接收精确 grant、bundle 和落地 manifest。它不能提供 inline source 绕行，
也不能从模型生成的路径构造 mount 或 policy。

## Provider 必需接口

推荐 provider 提供：

- 稳定的 `provider_id` 与 `supported_kinds = ("code_execution",)`；
- 返回真实可用性与能力事实的 `async_probe(...)`；
- `async_ensure(...)`、`async_health_check(...)`、`async_release(...)`；
- 实现 `async_execute_code(bundle, manifest, grant, timeout)` 的 resource；
- 只依据 `TaskWorkspaceAccessGrant` 翻译 Workspace root；
- argv-only 执行、有界输出、声明输出读回和完整清理。

`async_probe(...)` 应报告语言、真实观测的 toolchain version、Workspace access mode、
隔离、safety class、网络行为和机制专属事实。provider 按配置顺序确定性选择；不可用、
版本不合格或能力不合格的候选会记录原因并跳过。硬性 `isolation="required"` 永远不能选择
`trusted_local`。选中 provider 的事实会附在 Action result metadata 上，不能根据 provider
名称推断安全性。

对于 `code_execution`，`capabilities["isolation"]` 必须是观测到的布尔能力轴映射：
`process_contained`、`host_filesystem_restricted`、
`privilege_escalation_blocked`、`syscalls_restricted`，以及可选机制事实；旧字符串标签会被
拒绝。provider 还必须限制输出保留量，在超时和 coroutine cancellation 时停止所属进程/
容器，并显式暴露 `async_release(...)` 失败；manager 会隔离失败 handle，而不是把它标记
为已释放。

应用配置可使用字符串或候选描述符：

```python
agent.settings.set(
    "code_execution.providers",
    [
        {"provider_id": "preferred-provider", "config": {"profile": "strict"}},
        "docker",
    ],
)

agent.enable_code_runtime(language="python")
```

候选配置只在该候选被 probe 或 ensure 时合并，因此不同机制的 provider 配置不会
泄漏到核心 Action 契约。

容器运行时变体可以继承 `DockerExecutionResourceProvider`，只覆写
`create_resource(...)` 来构造自己的 `DockerExecutionResource` 子类。继承的 provider
复用 Workspace grant 绑定、镜像准备、健康检查与清理；有序选择与 ensure 前强制重新
probe 仍由 `ExecutionResourceManager` 负责。变体必须自行实现和实测运行时专属的 probe
事实与命令构造。这个 factory seam 不允许把模型生成的 Docker 参数注入执行路径。

## PR #325 的重构目标

gVisor 贡献仍归贡献者所有。基础契约落地后，请 rebase 或 retarget 原 PR，再把它改成
`code_execution` provider 或 Docker resource 的组合。重构后的 PR 应：

- 真实探测配置的 Docker binary、daemon 和目标 runtime；
- 在 probe/handle 事实中报告 active runtime；
- 通过 `create_resource(...)` 构造运行时专属资源，不复制 Docker provider lifecycle；
- 从 Workspace grant 推导 mount；
- 消费 adapter 生成的 build/run steps 与不可变 source bytes；
- 目标 runtime 缺失时 fail closed，不能静默使用默认 Docker runtime；
- 把具体 runtime 实现和真实测试保留在 PR #325。

基础分支刻意不包含复制来的 gVisor 命令或 provider 实现。

## PR #327 的重构目标

Seatbelt 贡献同样保留贡献者所有权。请把它重构为有稳定 `provider_id` 的
`code_execution` provider。该 PR 应：

- 探测真实平台和 policy executable；
- 从已解析 grant roots 生成 policy，不接受任意附加规则；
- 按 grant 保持 source 只读、build/output/logs 可写；
- 使用 async、argv-only 进程执行，并限制 stdout/stderr；
- 校验 realpath containment，并在成功、失败、超时、取消路径都清理临时 policy；
- 把具体 profile 实现和真实测试保留在 PR #327。

基础分支刻意不包含复制来的 Seatbelt profile 或 provider 实现。

## PR 验收检查表

- 不引入平行 Workspace、sandbox manager、session lifecycle 或资源 kind。
- 模型可见 Action input 不含 raw source path、raw command 或 provider-specific mount。
- probe 事实来自真实观测；synthetic fixture 必须明确标记。
- toolchain facts 使用 canonical tool id（`python`、`node`、`go`、`c++`）和标准化的
  观测版本，以便执行最低/精确版本约束。
- provider 通过通用 external-provider contract tests，并在自己的 PR 通过真实机制测试。
- 文档明确说明 safety class 与 fallback 行为。
