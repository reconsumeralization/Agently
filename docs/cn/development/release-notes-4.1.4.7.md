---
title: Agently 4.1.4.7 发布说明
description: Stage 0.3.8 集成、provider-neutral 隔离代码执行、reasoning 观测和 validation 诊断。
keywords: Agently, 4.1.4.7, Agently-Stage, TriggerFlow, gVisor, Seatbelt, Landlock, reasoning, validation
---

# Agently 4.1.4.7 发布说明

Agently 4.1.4.7 是基于公开 PyPI 4.1.4.6 的运行时集成与诊断版本：

- 要求 `agently-stage >=0.3.8,<0.4.0`，提供安全的混合同步/异步路由和
  Python 3.14 task factory 兼容；
- 在既有 provider-neutral `code_execution` 选择合同下注册内置 gVisor、
  macOS Seatbelt 与 Linux Landlock 候选；
- 把 provider 提供的 reasoning 保留为 result 与 RuntimeEvent 事实，并在
  provider 明确提供时记录 reasoning token 用量；
- 让 ModelRequest validation 失败和 retry 转换在 simple/detail 控制台诊断中可读。

这些隔离 provider 类随包提供并以未激活状态注册；外部机制只在显式选择时 probe。
它们不会增加 gVisor、Seatbelt 或 Landlock 第三方 Python 依赖。

## 核心变化

| 区域 | 变动内容 | 推荐用法 | 兼容性 / 风险 | 证据 |
|---|---|---|---|---|
| 同步 TriggerFlow chunk | provider-owned 同步 wrapper 可以用 `with Stage()` 调用异步 SDK，随后重新进入 `data.set_state(...)`、`append_state(...)` 或 `del_state(...)`。Stage 0.3.8 保留 0.3.7 的传递等待链路由修复，并转发 Python 3.14 task-factory 关键字参数。 | 保留有意设计的同步 provider 接口；工作必须停留在 caller-owned loop 时使用原生 async chunk。 | 不要求 provider 改写 API，也不要求它知道 TriggerFlow 私有使用 Stage。 | TriggerFlow execution-state 测试、Stage support contracts、`examples/trigger_flow/automatic_stage_sync_provider.py` |
| Stage 治理 | Stage 是必需运行时伴随仓，Agently 最低要求提高到 `>=0.3.8,<0.4.0`。 | 正常安装 Agently；仅当应用自身需要 Stage adapter 时直接从 `agently_stage` 导入。 | Stage 类型和 carrier 状态仍是机制层私有事实，不序列化，也不由 Agently 重新导出。 | 已发布 Stage 0.3.8 制品、依赖锁、兼容性清单 |
| Provider-neutral 代码执行 | 内置 provider id `gvisor`、`seatbelt`、`landlock` 进入既有有序 provider-candidate 协议。provider 模块默认未激活；外部二进制或 kernel 能力在选中时 probe。 | 通过 `enable_code_runtime(..., providers=[...], isolation=...)` 选择。 | 不增加第三方 Python 依赖。每个显式 provider 都 fail closed，不回退到 Docker 或 `trusted_local`；能力以实际观测轴为准，不能从名称推断。 | Provider-neutral 选择测试与各 provider conformance/integration 测试 |
| gVisor | `gvisor` 候选会在执行前验证 Docker 和实际可用的 `runsc` runtime。 | 宿主已配置 runsc 时使用 `providers=["gvisor"]` 与 `isolation="required"`。 | runsc 缺失、格式错误或不可执行均为终止错误；不会回退到 runc/宿主执行。 | gVisor provider/integration 测试与 Ubuntu workflow |
| macOS Seatbelt | `seatbelt` 候选只从 TaskWorkspace grant 派生可写规则，并默认禁止网络。 | macOS 使用 `providers=["seatbelt"]` 与 `isolation="preferred"`。 | 初版 profile 允许较宽宿主读取，并上报 `host_filesystem_restricted=false`；需要宿主读取隔离时使用 Docker/gVisor。 | Seatbelt bug/integration 测试与双语 ExecutionResource 文档 |
| Linux Landlock | `landlock` 候选通过 provider-owned helper 应用 `PR_SET_NO_NEW_PRIVS` 和 ABI-aware 文件系统规则。 | 支持 Landlock 的 Linux kernel 使用 `providers=["landlock"]` 与 `isolation="preferred"`。 | Landlock 限制文件系统，但不隔离 process、network 或一般 syscall；不支持的 kernel 会 fail closed。 | Landlock isolation/integration 测试与 Ubuntu workflow |
| Reasoning 观测 | `ModelRequestResult.get_data(type="all")` 保留 `reasoning_delta` 和 nullable `reasoning`；Event Center 发布 `model.reasoning.delta` 与 `model.reasoning.completed`；provider 明确提供的 `reasoning_tokens` 保持独立用量明细。 | 将其作为观测/审计事实，不反馈给 prompt、routing、retry 或质量判断。 | reasoning 缺失时保持 null/unknown；Agently 不推断隐藏 chain-of-thought，也不从文本估算 reasoning tokens。 | ModelRequest 观测、structured-output 测试及 DevTools companion 测试 |
| Validation 诊断 | simple model logs 展示 validator、reason、attempt 和 retry 转换；detail 可增加有界 validation context 与 traceback tail，同时不重复 response 或 reason。 | 使用 `debug=True` 获取简洁诊断，使用 `debug="detail"` 做有界深度检查。 | 日志只负责观测；确定性 validation 与 retry 合同仍是权威。 | Runtime console/event 测试与 settings 文档 |
| Workflow ownership | TriggerFlow 继续拥有 workflow state、生命周期、持久化、并发与错误。 | 继续使用 TriggerFlow 公开 execution API。 | Stage carrier 细节保持进程内私有且不序列化。 | 兼容性清单与 TriggerFlow 生命周期测试 |

## Provider-owned 同步接口

底层 SDK 为异步实现时，Tool 或 Function provider 仍可保留有意设计的同步方法：

```python
from agently_stage import Stage


def search(query: str):
    with Stage() as stage:
        return stage.get(search_tool.search, query)


def chunk(data):
    result = search(data.input)
    data.set_state("search_result", result, emit=False)
    return result
```

这仍是会阻塞调用 worker 的同步边界。原生 async chunk 继续使用 `await` 与
`data.async_set_state(...)`。

Agently 不重新导出 Stage。直接标量 adapter 使用：

```python
from agently_stage import Stage

sync_search = Stage.as_sync(search_tool.search)
async_transform = Stage.as_async(transform)
```

stream 转换、Stage/executor 注入、独立 close 和显式轻桥接继续由
`StageCallBridge` 负责。

## Provider-neutral 隔离选择

使用既有通用 API，不增加 provider-specific Agent 方法：

```python
agent.enable_code_runtime(
    language="python",
    providers=["gvisor"],
    isolation="required",
)
```

部分宿主机制在 macOS 选择 `seatbelt`、在 Linux 选择 `landlock`，并使用
`isolation="preferred"`。选中的 handle 与 Action result 会记录实际观测到的
toolchain、safety、isolation axis 与 fallback 事实。

## Reasoning 与 validation 诊断

Reasoning-capable result 可以在不把 reasoning 混入答案 parser 的情况下读取：

```python
result = agent.input("解释该决策。").get_result()
all_data = result.get_data(type="all")
print(all_data.get("reasoning"))
```

排查输出合同失败时开启有界 validation 诊断：

```python
agent.set_settings("debug", "detail")
```

DevTools `0.1.11` 增加对应的 reasoning view、显式 reasoning-token 聚合和有界的
run-partitioned observation ingest pipeline，同时保持
`agently-devtools.observation-runtime.v1`。

## 升级

安装框架与匹配的可选 DevTools 版本：

```bash
pip install -U "agently==4.1.4.7"
pip install -U "agently-devtools==0.1.11"
```

- Python：`>=3.10`
- Agently-Stage：`>=0.3.8,<0.4.0`
- 推荐 Agently DevTools：`>=0.1.11,<0.2.0`
- Skills authoring protocol：`agently-skills.authoring.v2`
- DevTools observation protocol：`agently-devtools.observation-runtime.v1`

Stage 调用链不要求应用源码迁移。选择新隔离 provider 的应用必须满足其宿主前置条件，
并且不能把 provider 名称当作强于实际 capability axes 的隔离证明。
