---
title: 项目结构
description: 面向 Agently 应用与服务的拓扑先行、简洁项目结构指导。
keywords: Agently, 项目结构, 拓扑, TriggerFlow, Dynamic Task, FastAPI, FastMCP
---

# 项目结构

> 语言：[English](../../en/start/project-framework.md) · **中文**

目录树是设计结果，不是设计起点。先明确真实 owner 与 consumer，再创建承载这些
边界所需的最小文件结构。

完整可运行示例见 Agently-Skills 的
[`skills/agently/assets/project-template`](https://github.com/AgentEra/Agently-Skills/tree/main/skills/agently/assets/project-template)
资产。它为演示目的同时包含多种可选边界；小型应用应按需取用，不要把它当成必须
整套复制的脚手架。

## 先规划拓扑，再创建文件

任何非简单的线性、分支、并发或循环模型应用，在创建模块前都应先写四张 ledger：

1. **owner / invariant ledger**：模型、宿主、Action、flow、存储、传输或人工分别
   拥有哪些决策，以及必须保持什么不变量。
2. **planned node ledger**：每个逻辑 ModelRequest 或宿主阶段的 input、精确 output
   schema、证据边界、生命周期与拆分理由。
3. **planned edge ledger**：每个 value、state、signal、effect 与用户投影的 producer、
   校验或转换，以及 consumer。
4. **production-necessity ledger**：每个节点和字段为什么存在、谁消费、可见性与
   保留策略、失败行为，以及声称的质量收益是 hypothesis、observed 还是 A/B verified。

规划图中的一个节点不等于一个 Python 文件。几个小型宿主校验可以留在所属 Chunk
旁边；真正可复用的外部契约可以独立成模块。物理文件按 owner 边界划分，不按图框
数量划分。

运行时图、event、trace 与 artifact 用来验证规划拓扑，不能替代规划。邻接关系只能
证明阶段被激活，不能证明正确字段已经传给正确 consumer。

## 从最小诚实结构开始

### 单个请求族

```text
project/
├── app.py
├── SETTINGS.yaml
├── prompts/
│   └── request.yaml
└── tests/
```

请求很小且只有一个 consumer 时，直接保留在 `app.py`。只有请求模块真正拥有可复用
Prompt/output contract 或有意义的宿主校验时才拆出文件。不要为了看起来完整而加入
TriggerFlow、`services/`、`domain/`、`tools/` 或空 package。

意图识别、语义路由、规划、权衡、应答生成与质量判断属于模型语义工作；schema、
enum、可信 key 集合、授权、硬策略、生命周期、canonical identity 重建与副作用属于
宿主。模型参与不代表每个语义步骤都必须单独创建一个 ModelRequest。

### 稳定的多阶段工作流

```text
project/
├── app.py
├── SETTINGS.yaml
├── TOPOLOGY.md
├── prompts/
├── workflows/
│   ├── main_flow.py
│   └── chunks/
└── tests/
```

开发者拥有的稳定拓扑使用 TriggerFlow。`TOPOLOGY.md` 保存四张 ledger；
`main_flow.py` 拥有图与 execution lifecycle；每个必要 Chunk 对应一个可独立观测的
业务阶段。如果 `for_each(...).end_for_each()` 已经返回 join 后的列表，且没有额外
转换、部分失败或策略边界，就不要再创建单独的 join Chunk。

实现前先区分有序与独立工作。独立阶段优先使用 async API 与有界并发。只有真实
数据依赖、顺序保证、副作用安全或外部容量约束才应串行。压力控制放在真正 owner：
宿主 admission、TriggerFlow execution、`batch`/`for_each`、模型 scheduler、client
连接池，或阻塞代码的 thread pool。

### 提交或模型生成的 DAG

```text
project/
├── app.py
├── SETTINGS.yaml
├── task_dag/
│   ├── contracts.py
│   ├── handlers.py
│   └── runtime.py
└── tests/
```

plan 是运行时数据时使用 TaskDAG / Dynamic Task。先通过 TaskDAG 路径校验并解析，
再让 `TaskDAGExecutor.async_run(...)` 使用 TriggerFlow substrate。不要把未经校验的
提交式或模型生成 plan 直接编译成新的 TriggerFlow definition。只有明确需要 Blocks
lifecycle evidence 或 `ExecutionBlockGraph` 输出时才显式选择 Blocks。

## 只在需要时加入传输入口

同时暴露 HTTP 与 MCP 的应用可以增加：

```text
services/
├── contracts.py      # 两种传输真正共享时的公开投影
├── api.py            # 直接 FastAPI inbound adapter
└── mcp_server.py     # 直接 FastMCP server adapter
```

两种 adapter 都应校验 admission、签发宿主 task identity、调用同一个 async 应用入口，
并返回同一份经过批准的公开投影；transport 不应成为 workflow policy owner。

```python
from uuid import uuid4


# services/api.py
@app.post("/analysis", response_model=AnalysisResponse)
async def analyze(request: AnalysisRequest) -> AnalysisResponse:
    task_id = f"analysis-{uuid4().hex}"
    run = await run_analysis(
        request.question,
        task_id=task_id,
        max_concurrency=request.max_concurrency,
    )
    return project_analysis_run(task_id, run)


# services/mcp_server.py
@mcp.tool
async def analyze_with_mcp(
    question: str,
    max_concurrency: int = 4,
) -> dict[str, object]:
    request = AnalysisRequest(
        question=question,
        max_concurrency=max_concurrency,
    )
    task_id = f"analysis-{uuid4().hex}"
    run = await run_analysis(
        request.question,
        task_id=task_id,
        max_concurrency=request.max_concurrency,
    )
    return project_analysis_run(task_id, run).model_dump()
```

可运行模板包含完整 import、settings lifespan、task-local path 和进程内 transport
测试。上面的缩略代码只展示 ownership 关系。

如果项目明确需要其封装好的 task/stream protocol，仍可使用 `FastAPIHelper`；它没有
被 deprecated，但普通 typed HTTP route 默认直接使用 FastAPI。MCP client 消费已经由
Agently Action 管理负责，不要再添加本地 MCP-client service 或只转发注册调用的 wrapper。

## 保持 Prompt 与输出契约明确

需要独立演进的稳定 Prompt contract 放入 YAML 或 JSON：

- `input`：当前运行事实；
- `info`：权威事实、API/schema 文档、signature、docstring、evidence 与可选 key 集；
- `instruct`：转换与调用规则；
- `output`：精确的机器可消费结果。

每个下游消费字段都要说明 type、语义、requiredness、enum 或 format、range、
nullability 与跨字段约束。实际外部调用或副作用前，宿主仍必须校验结果。

模型选择宿主 record 时，只提供一个可信 selection key 与任务相关事实。宿主校验
key 属于本轮 offered set 后，再确定性重建 canonical id 与 metadata。不要让模型抄写
UUID、多组 id、URL 或无关 metadata。

不得请求或保存隐藏思维链。只有明确注释语义角色、证据边界、类型和范围、consumer、
可见性、保留策略、失败行为与质量证据状态时，才可以使用有界的任务特定过程字段。
通用且未消费的 `reasoning`、`analysis` 或 `thinking` 字段不是质量机制。

## 相关信息就近聚合

一起变化、服务同一 consumer 的信息应尽量就近聚合。无论人还是 Coding Agent，理解
一个请求、不变量或副作用所需的跨文件检索次数与嵌套深度都应尽量降低。不要只为了
缩短当前代码或让目录形式看起来分层完整，就把只使用一次的 schema、常量、helper
或 class 搬到别处。

这不意味着创建 god module。无关职责仍应拆分；只有新边界真正拥有复用、独立版本或
review、policy/lifecycle、非平凡表示转换或动态组装时才抽取，并让调用点可以直接定位
到该 owner。目标是可读的内聚，而不是最大程度内联。

## 删除没有 owner 的 wrapper

新增 Service、Manager、Factory、request wrapper、repository facade 或 adapter 前，
至少要证明它拥有一项真实责任：

- authorization、validation、policy 或 safety；
- lifecycle、state、cleanup、retry、concurrency 或 transaction scope；
- 稳定外部 contract 或非平凡 representation translation；
- 完全相同 contract 的多个 consumer；
- 已发布的 compatibility boundary。

否则就内联。删除只改名的函数、只转发的 manager、空 package、无 consumer 的 output
node 和重复 facade。简洁是 owner 与 consumer 属性，不是统一行数限制。

## Result、state 与 evidence 边界

- 没有任何调用方消费渐进输出时，直接等待 `async_get_data()`；不得用 no-op loop
  排空 `instant` generator。
- 已消费的 `instant` 字段仍是 provisional；不可逆副作用必须等待最终解析与校验。
- 单次 TriggerFlow run 的数据放 execution state。即使 save/load 会序列化并替换其值，
  `flow_data` 仍然共享在 flow object 上。
- observation、external emit、pause/resume、save/load、intervention、cancellation 或
  host-controlled close 使用显式 execution handle。
- trace 记录有界事实，Eval 判断语义质量；不要在每个 event 重复完整 Prompt、delta、
  secret 或 raw metadata。

先测试确定性契约：settings、Prompt/output schema、宿主校验、TriggerFlow state 与 join、
TaskDAG admission、service projection 和 trace allowlist。Mock 只能证明 wiring，不能证明
模型语义；可执行 Prompt 或语义行为变化仍需要明确 criteria 与最小、已授权的代表性
真实模型校验。

## 另见

- [设置](settings.md)
- [Prompt 管理](../requests/prompt-management.md)
- [Schema as Prompt](../requests/schema-as-prompt.md)
- [TriggerFlow 概览](../triggerflow/overview.md)
- [Dynamic Task](../dynamic-task/README.md)
- [FastAPI 服务封装](../services/fastapi.md)
