---
title: Agently 4.1.4.4 发布说明
description: 更完整的 Pydantic 输出约束、具备恢复语义的 TriggerFlow 快照，以及线上模型优先的发布校验。
keywords: Agently, 4.1.4.4, Pydantic, 结构化输出, TriggerFlow, 快照, RecordStore
---

# Agently 4.1.4.4 发布说明

Agently 4.1.4.4 加强了两个既有 owner 边界：`ModelRequest` 继续负责结构化
输出提示词与校验；TriggerFlow 及其配置的持久化 provider 继续负责可恢复执行
快照。本版本没有增加新的 facade 或平行 runtime。

## 结构化输出约束与修正

Pydantic v2 字段约束现在会进入支持的结构化输出提示词格式，包括必填性、
可空性、alias、enum/literal、字符串与集合长度、数值范围、pattern 和 format。
原始模型仍然是最终接受权威。

```python
from pydantic import BaseModel, Field


class Ticket(BaseModel):
    title: str = Field(min_length=3, max_length=80)
    priority: int = Field(ge=1, le=3)
    labels: list[str] = Field(min_length=1, max_length=5)


ticket = (
    agent
    .input("把这份事故报告整理为分诊工单。")
    .output(Ticket, format="json")
    .get_result()
    .get_data_object()
)
```

解析结果未通过 Pydantic 校验时，Agently 会把有界的字段级修正信息送入既有
retry 路径。不合规 dict 不会作为成功业务数据返回；接受后的重试结果仍可由
object、data 和 text result reader 复用。

## 具备恢复语义的 TriggerFlow 快照

Issue [#331](https://github.com/AgentEra/Agently/issues/331) 证明，反复
save/load 可能在每份恢复快照中保留大型已完成 signal value。4.1.4.4 为符合
条件的终态值增加了可选 projection policy：

```python
execution.set_snapshot_projection_policy(
    terminal_value_mode="digest",
    min_value_bytes=4096,
)
```

待恢复 interrupt 和恢复所需 state 始终保持完整。终态投影保留规范 SHA-256
digest 与编码大小，因此仍可检查重复或冲突的 resume request。schema-v2
快照可以加载既有 schema-v1 全值快照。

内置本地 `RecordStore` 现在默认按 `run_id` 保留最新三份执行快照：

```python
record_store = RecordStore(
    "./recovery",
    snapshot_retention={"keep_last": 5},
)

execution.set_snapshot_retention_policy(keep_last=2)
report = await execution.async_prune_recovery_snapshots(keep_last=1)
```

使用 `{"keep_last": None}` 可以关闭自动清理。projection 由理解恢复语义的
TriggerFlow 负责；物理 retention 由持久化 provider 负责。通用
`put_checkpoint(...)` 写入不会被自动清理。

## 发布校验策略

默认 `pytest` 不再依赖本地 Ollama 服务或预先拉取的 Ollama 模型。确定性的
OpenAI-compatible 协议测试仍保留在常规测试套件中。真实模型发布证据单独
使用显式配置的线上模型运行，从而如实记录 provider、model、请求次数和观察
结果。

## 核心变更与升级影响

| 范围 | 变更 | 推荐用法 | 兼容性与风险 | 证据 |
|---|---|---|---|---|
| 结构化输出 | 支持的 Pydantic 字段约束进入提示词，校验失败进入有界 retry。 | 保持以 `BaseModel` class 作为 `.output(...)` contract。 | 对原先约束表达不足的增量修正；自定义 validator 仍只在 host 端执行。 | Prompt generator、validation、result reuse 与 typing tests。 |
| 快照投影 | TriggerFlow 可以 digest 符合条件的终态 interrupt value 与已完成 resume metadata。 | 当已完成值主导快照大小时显式启用。 | 默认仍保存全值；待恢复数据绝不投影。 | Issue #331 A/B：默认 1,307,086 B，对比 digest projection 106,782 B，减少 91.83%。 |
| 快照保留 | 内置本地 provider 默认保留最新三份版本。 | 配置 provider 默认值或 execution override；维护时使用显式 prune。 | 有意调整本地 provider 默认行为；`keep_last=None` 可保留全部版本。 | Retention、override、save/load、registry 与 provider-port tests。 |
| 模型校验 | 默认 `pytest` 移除本地 Ollama 调用。 | 使用显式、有界的线上模型实验形成发布证据。 | 仅测试策略变化；Ollama 仍可作为 OpenAI-compatible endpoint 配置使用。 | 确定性 mock coverage 与发布证据记录。 |
| 延后 | 不包含全快照字节上限、artifact ref 外置和分布式 provider retention 实现。 | 大型业务 artifact 不应进入恢复快照；每个持久化 provider 在自身边界实现 retention。 | 不承诺通用快照大小上限。 | #331 实验和 owner-boundary review 明确记录的限制。 |
| 延后 | #324 跟踪的 gVisor 与 Seatbelt 具体 provider 仍由贡献者负责。 | 使用已发布的 provider-neutral `ExecutionResource` seam 或显式授权的 provider。 | 不是 4.1.4.4 core runtime blocker；不打包未评审的 sandbox 实现。 | Issue #324 与 PR 状态检查。 |

## 校验

已观察到的 release candidate 校验结果：

- 对 `agently/`、`tests/`、`examples/` 的源码 Pyright：0 errors；
- 干净 worktree 默认套件：2,438 passed、27 skipped；其中 25 项是维护者本地
  spec runner 检查，挂载嵌套 spec repository 后 25 项全部通过；剩余两项需要
  可选的 Anthropic Skills checkout；
- 三个 release-pinned 确定性用法脚本全部通过；
- TriggerFlow durable-recovery 示例保持 load、latest-N retention、显式 prune、
  幂等 resume 和 durable event 效果；
- wheel 与 source distribution 构建成功；全新 Python 3.10 环境成功安装
  wheel，确认 `py.typed`、结构化缺失依赖错误，并通过安装包外部 Pyright smoke；
- 一次有界 DeepSeek `deepseek-v4-flash` 请求在一次调用中返回声明的 Pydantic
  模型，保持 priority 与 labels，并满足全部长度、数量和范围约束。invalid-first-
  attempt correction 与 retry reuse 仍由确定性测试证明。

## 兼容性

- Package version：`4.1.4.4`。
- Release manifest：`compatibility/releases/4.1.4.4.json`。
- Python：`>=3.10`。
- 推荐 DevTools 版本保持 `agently-devtools >=0.1.10,<0.2.0`。
- Skills authoring protocol 保持 `agently-skills.authoring.v2`。
