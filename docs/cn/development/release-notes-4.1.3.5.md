---
title: Agently 4.1.3.5 Release Notes
description: Agently 4.1.3.5 的 settings-owned 结构化输出默认值、有意义必填值、AgentTurn prompt 隔离和 set_turn_prompt 语义 release note。
keywords: Agently, release notes, 4.1.3.5, structured output, AgentTurn, set_turn_prompt, DevTools
---

# Agently 4.1.3.5 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.5.md) · **中文**

Agently 4.1.3.5 是一版 request foundation 稳定性切片。重点处理两个根合同：
结构化输出解析在本地模型上要稳定；同一个 Agent 实例可以复用，但每个 turn 的
request prompt 必须隔离。

## 变更内容

### 结构化输出默认值由 settings 持有

省略 `.output(..., format=...)` 时，现在会从当前 settings 链读取
`prompt.default_output_format`。全局默认仍是 `json`。如果调用方已经验证过目标模型
和 schema family，仍然可以显式使用 `format="auto"` 或
`prompt.default_output_format="auto"`。

这版保留 `auto`，但不再把它当默认策略。近期本地 `qwen2.5:7b` 检查发现，
hybrid 风格响应可能遗漏必需 section header，或把 scaffold comment 写进文本字段，
所以框架默认保持保守。

### 必填字段必须有有意义的值

tuple `ensure=True` 和运行时 `ensure_keys` 现在要求目标路径解析到有意义的值。
缺失 key、`None`、空白字符串、空 wildcard 匹配，以及 wildcard 匹配结果里包含空白
必填值，都会触发校验失败并消耗正常 retry budget。`False` 和 `0` 这类 typed falsey
值仍然有效。

### AgentTurn 持有 request-scoped prompt draft

非 `always=True` 的 Agent quick prompt 链会创建隔离的 `AgentTurn` request draft。
表达式内链式调用仍然简洁：

```python
result = await agent.input("Review this ticket.").output({
    "answer": (str, "final answer", True),
}).async_start()
```

如果配置拆成多条语句、条件分支或 helper 函数，需要显式持有 turn：

```python
turn = agent.create_turn()
turn.input("Review this ticket.")
turn.output({"answer": (str, "final answer", True)})
result = await turn.async_start()
```

### `set_turn_prompt(...)` 命名单 turn 写入表面

`set_turn_prompt(...)` 是现在推荐的单 turn prompt slot 写入名称。
`set_request_prompt(...)` 保持为行为一致的兼容别名，不废弃。

Prompt 配置文件现在推荐用 `.turn` 表示 turn-scoped section；`.request` 继续兼容。

```yaml
.turn:
  input: Review this ticket.
  output:
    $format: json
    answer:
      $type: str
      $ensure: true
```

### Bash sandbox action 描述包含策略边界

`register_bash_sandbox_action(...)` 和 `agent.enable_shell(...)` 现在会把
模型可见的命令前缀 allowlist、允许的工作目录根路径和 timeout 追加到 action 描述中。
当 shell action 暴露给模型时，能力边界会随 desc 一起可见。

## 兼容性

- Package version: `4.1.3.5`。
- Release manifest: `compatibility/releases/4.1.3.5.json`。
- 推荐 `agently-devtools`: `>=0.1.7,<0.2.0`。
- `set_request_prompt(...)` 和 `.request` prompt config 继续兼容。

## 验证摘要

- 使用本地 `qwen2.5:7b` 对课程脚本做 editable install smoke，多次结构化输出检查通过，
  未出现 key section 缺失或 scaffold comment 残留。
- 目标回归测试覆盖 settings-owned 输出默认值、有意义必填值、AgentTurn request-scoped
  prompt draft 和 `set_turn_prompt(...)` 兼容行为。
- 静态类型回归覆盖 AgentTurn 转发、ModelResponse stream facade、`specific`
  事件元组、AgentExecution stream item，以及从 `agently` 和
  `agently.types.data` 暴露的 Skills stream handler 别名。
- Agently-Skills 指引已更新，并通过 companion validation suite。

## 延期范围

本版不合并 `response` 和 `result`。`ModelResponse` 已经代理常用 result reader；
`response.result` 继续作为 text、data、metadata、validation 和 streaming 的稳定
cache/materialization facade。
