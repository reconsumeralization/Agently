---
title: Agently 4.1.4.7 发布说明
description: TriggerFlow 集成中的 Stage 混合同步/异步环境自动路由。
keywords: Agently, 4.1.4.7, Agently-Stage, TriggerFlow, sync, async, carrier
---

# Agently 4.1.4.7 发布说明

Agently 4.1.4.7 将 Stage 最低依赖提升为
`agently-stage >=0.3.7,<0.4.0`，并修复针对
[Agently #347](https://github.com/AgentEra/Agently/issues/347)、
[Agently-Stage #24](https://github.com/AgentEra/Agently-Stage/issues/24) 与
[Agently-Stage #25](https://github.com/AgentEra/Agently-Stage/issues/25)
所暴露运行时问题的 Agently 集成。

根因修正在 Stage：同步与异步 scope 现在会区分继承的逻辑执行 lineage，以及当前调用
在物理上可以安全阻塞的线程和 event loop。Stage 0.3.7 还会传播传递同步等待链上的
全部上游 carrier，避免嵌套 scope 重新选中一个正在间接等待自己的 loop；它取代了
0.3.6 中不完整的路由修正。4.1.4.7 增加完整 TriggerFlow 链路的回归契约；
后续 Agently 安装会默认取得完整修正。

公开 PyPI 基线是 4.1.4.6。本次是基于该公开版本的聚焦运行时集成补丁。

## 开发者可见变化

| 区域 | 变动内容 | 推荐用法 | 兼容性 / 风险 | 证据 |
|---|---|---|
| 同步 TriggerFlow chunk | 工具提供方的同步 wrapper 可以用 `with Stage()` 调用异步 SDK，随后继续调用 `data.set_state(...)`、`append_state(...)` 或 `del_state(...)`。 | 保留同步 provider 接口；仅在工作必须停留于 caller-owned loop 时使用原生 async chunk。 | 不要求提供方改写 API，也不要求其知道 TriggerFlow 私有使用 Stage。 | `tests/test_cores/test_trigger_flow_execution_state.py`；`examples/trigger_flow/automatic_stage_sync_provider.py` |
| Stage 依赖 | 在既有 `<0.4.0` 兼容线内将最低版本提升为 `0.3.7`。 | 安装 `agently==4.1.4.7`（或更高的兼容版本）。 | 修正未来安装的依赖解析；直接使用 Stage 的应用不应降级到 0.3.7 以下。 | `pyproject.toml`；`poetry.lock`；`tests/test_stage_support_contract.py` |
| `FunctionShifter.syncify/asyncify` | Deprecated 名称与警告保留；标量调用委托给 `Stage.as_sync/as_async`。 | 保留既有 import 与调用形式，或迁移到 `Stage.as_sync/as_async`。 | 兼容 façade 仍受支持。 | `tests/test_utils/test_function_shifter.py` |
| 内部 bridge | Agently 需要轻桥接、注入生命周期、stream 或显式 `managed=True` settlement 的位置继续使用 `default_stage_call_bridge`。 | 不要把所有 bridge 调用替换成 scoped adapter。 | 不改变这些已有边界的语义。 | `agently/utils/FunctionShifter.py`；Stage support contracts |
| Workflow ownership | TriggerFlow 继续拥有 workflow state、生命周期、持久化、并发与错误。 | 继续使用 TriggerFlow 公开 execution API。 | Stage carrier 细节保持私有且不会序列化。 | `compatibility/releases/4.1.4.7.json` |

## 工具提供方拥有的同步接口

即使底层实现是异步的，工具或 Function 提供方仍可有意保留同步方法：

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

Stage 会自动选择物理上安全的 carrier。工具提供方无需探测 TriggerFlow 是否已在内部
使用 Stage。

这个调用边界仍会同步阻塞所在 worker。原生 async chunk 仍应优先使用 `await` 与
`data.async_set_state(...)`。依赖 caller loop-bound 对象的工作必须留在其 owner loop
并在那里 await。

## Stage 接口

Agently 不会重新导出 Stage。应用需要直接使用 scoped adapter 时，从
`agently_stage` 导入：

```python
from agently_stage import Stage

sync_search = Stage.as_sync(search_tool.search)
async_transform = Stage.as_async(transform)
```

每次 adapter 调用拥有一个自动 Stage scope，并等待结果及 Stage-owned settlement。
stream 转换、Stage/executor 注入、独立 close 与轻桥接继续由高级接口
`StageCallBridge` 负责。

## 升级

报告中的调用链不需要修改应用源码。正常安装本发布：

```bash
pip install -U "agently==4.1.4.7"
```

已有 Agently 安装如果已允许 Stage 0.3 兼容线，也可以独立将 Stage 升级到 0.3.7；
但安装 Agently 4.1.4.7 才是前移最低依赖的受支持方式。
