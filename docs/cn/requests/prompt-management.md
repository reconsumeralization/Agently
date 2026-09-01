---
title: Prompt 管理
description: 分层 prompt 槽位、agent 与 request 作用域、YAML/JSON 加载与占位符。
keywords: Agently, prompt, role, system, info, instruct, input, configure_prompt
---

# Prompt 管理

> 语言：[English](../../en/requests/prompt-management.md) · **中文**

Agently 把 prompt 拆成命名槽位。槽位可组合，所以 agent 级持久内容只设一次，请求级槽位每次按需填。

## 槽位映射

| 槽位 | 落在哪 | 典型用途 |
|---|---|---|
| `role` / `system` | system 消息 | 角色、能力边界 |
| `info` | system 或 user（实现细节） | 背景事实、目录、工具清单 |
| `instruct` | user 消息 | 这类请求的步骤指令 |
| `input` | user 消息 | 实际问题或 payload |
| `output` | user 消息 + parser | 期望的返回结构 |

## Prompt 协同设计与审校流程

当已经明确用户使用 Agently 开发，并且当前工作是方案设计、流程/区块优化或 Prompt 审查时，
默认主动采用本方法，不等用户另外要求表格。普通实现、Bug 修复、provider 配置或无关设置
不会仅因仓库使用 Agently 就触发这套审校流程。用户明确要求批量审阅、跳过细节或委托决定
时，遵从该节奏。

围绕复杂业务流程或其中一个区块开展协同设计、方案检查时，先说明整体场景，请用户确认
涉及的逻辑 ModelRequest 清单及每个请求的职责。可以用简洁表格展示职责、主要输入、
输出消费者与依赖，复用已有拓扑计划；区分模型与宿主工作，也区分重复执行的请求类型和
provider 重试次数。

清单确认后，根据用户需要选择细审请求，或说明某个请求为什么必须确认，例如存在未决
业务选择、重要政策边界或影响较大的输出契约。默认每次只呈现一个选定请求的设计，等待
用户确认或提出修订，再进入下一项或将该设计视为已批准。用户明确要求批量审阅或委托
决定时，可以调整这个节奏。

清单确认不等于全部单项 Prompt 已批准。修订应回到实际 chain/config，并检查受影响的
上下游契约和流程职责；范围或衔接变化时重新确认变化部分，不重复审阅未变化的决定。
在普通审阅记录中清楚标明待确认、已确认或修订中即可，不增加新 runtime 协议，也不要求
所有常规请求和小改动都逐项审批。

## 向业务用户展示 Prompt 设计

需要用户确认业务提示词方案时，可以先说明业务场景、本次请求具体处理什么问题、职责边界
以及谁会消费结果，再用简洁的 Agently 槽位表展示实际 prompt 内容和字段约束，不要求用户
到分散代码里拼出完整请求。

优先让表格承担主体信息：请求总览、`Slot | 主题 | 实际 Prompt 内容` 主表、按需出现的
可见示例表，以及输出字段的类型、必填性、含义与约束表。必要时补充枚举、格式、范围、
可空性和下游校验；按实际决策增减，不要仅用“这里放业务规则”代替具体设计文本。

### 长 slot 分块与示例展示

内容较多的 slot 应在真实 Prompt 和审阅视图中按段落、主题或章节组织，用主题行或展开
章节代替一个巨型单元格。这不意味着拆文件、拆请求或增加封装。逐块检查相关性、事实与
规则权威、重复冲突，以及是否把单个实例错误泛化成了规则。

会发送给模型的示例应展示具体内容、它解释的既有规则、实际所属 slot/config，以及合成
或脱敏来源性质。仅供审阅者看的备注、输出说明样例应另标为 **不发送给模型**，不默认把
审阅元数据注入 Prompt。Examples 是展示分组，不是新增 Agently slot/API；继续遵守
示例占比限制，也不为填满版式而强行添加示例。

可折叠内容以方便导航，但应能查看完整的获准内容，并标明省略与脱敏。展示仍与真实
chain/config 和渲染后的 Prompt 审计一致，不能另维护一份 Prompt 真值。

可参考[表格化协同设计样例](prompt-collaboration.md)。其中场景、请求清单和输出字段
仅用于演示呈现方式，不是强制业务模板；选入细审的请求仍按上述流程等待确认。

## 单次请求契约就近聚合

一次性的 Agently fluent request 应保持为一条可直接阅读的请求链：`input`、权威
`info`、`instruct`、`output` schema，以及 `get_result()`、`get_data()`、
`async_get_data()` 等终结结果调用都应在这条链上清晰可见：

```python
result = (
    agent
    .input({"ticket_text": ticket_text})
    .info({"allowed_queues": allowed_queues})
    .instruct("选择最合适的队列，并给出一条简洁说明。")
    .output({
        "queue": (str, "allowed_queues 中的一个值。", True),
        "explanation": (str, "用户可见的简洁说明。", True),
    })
    .get_result()
)
triage = await result.async_get_data()
```

一份 YAML/JSON Prompt Configure 文件配合显式 `mappings` 是等价的声明式表达。只有
schema 或 prompt 片段会被原样复用、由另一 owner 独立版本化或产品编辑，或者确实是
动态生成、条件组装时，才拆开这条请求链。不要仅仅为了让 Agently 请求链看起来更短，
就把只使用一次的 schema 或 prompt 步骤搬到别处。

## 请求本地上下文

每个模型可见的 prompt 项都必须承担至少一种当前请求角色：

1. 解释已提供的输入；
2. 提供权威事实、策略、schema 或证据项；
3. 改变模型拥有的决策或转换；
4. 定义输出、消费者、tool 或能力边界；
5. 提供有用的用户可见过程上下文、状态或说明，并声明对应的用户或 UI 消费者。

应有意识地使用 prompt 槽位：`agent` 提供稳定的角色与能力；`input` 提供当前事实；
`info` 提供权威契约与证据；`instruct` 提供任务规则；`output` 提供所需结果结构。
它们合在一起必须让模型获得对当前请求自包含的说明，而不是假定模型知道未解释的项目
上下文。

对每个候选内容使用移除反事实：若移除它不会改变当前请求的有效任务、契约、证据、
决策、允许的 verdict，或已声明的用户/UI 投影，就应移除或改写它。内容来自项目级并
不是移除的理由：只要共享策略或事实会改变本次请求，就应保留。当有效的上游调用方保证
会改变模型拥有的决策或允许的 verdict 集合时，应保留它，或把它改写为行为约束。专有
名称只有在标识会改变本次请求的真实领域契约、allowlist、证据项、输入事实或能力边界
时才可保留。否则，应把未解释的实现名称改写为它与请求相关的角色，或直接移除。第五种
角色不允许泛化的项目叙述：必须声明哪个用户或 UI 消费者会使用该过程上下文、状态或
说明。

| | `info` |
|---|---|
| 不好 | “遵循项目的 worker-manager 约定。” |
| 好 | “允许的操作：批准或拒绝。证据：附带的请求及其策略记录。” |

分两层审计：先按槽位角色和移除反事实检查每个槽位，再检查实际渲染的请求，包括
mappings 和引用。发送前，`execution.get_prompt_text()` 审计的是已渲染的 execution
draft，不是最终 ModelRequest prompt。当 TaskContext、Session、Skills、检索、Actions
或其他 runtime extension 还可能注入内容时，应在有界测试中检查注入后发出或构建的最终
ModelRequest `prompt_text`，例如 `prompt.built` 事件的 `payload.prompt_text`。不得把
启动后的 execution snapshot 当成 late injection 的充分证据。保留 prompt 证据前必须
脱敏秘密信息。

## 规范性指令中禁止业务特例

不能仅因为单个观测实例暴露了失败，就把其中的实体字面量、一次性输入或环境状态、
历史事故、测试样例或 fixture、预期答案及其对应行为直接升级为规范性 prompt 分支。
应先定位缺失的通用不变量或决策边界，写出覆盖该类问题的最小通用规则，再用原实例以及
对照的正常、异常和边界案例验证。除非这些字面值是当前请求真实提供的事实，否则应从
规则中移除。

这不代表删除真实业务上下文。只要当前有效的业务策略、领域不变量、授权规则、接口契约
或运行时事实会改变本次请求，就仍应按 owner 放入 `info`、`instruct`、`input`
或 `output`，不能把必要合同误称为特例。

示例不具有规范性。它只能解释已经明确写出的通用规则，不能引入正文里不存在的行为、
优先级、例外或预期答案。示例应清楚标记，优先使用通用或合成内容；单侧示例可能造成
错误默认时，应提供对照示例。

作为 Agently prompt 审查规则，最终渲染 prompt 中的示例内容总量必须小于非示例的
规范性正文。两侧应统一使用字符数或模型 token 计量。这个比例是工程侧 authoring
门槛，不代表模型注意力存在普适的 50% 临界值。

如果任务看起来需要更多 demonstrations，应把它作为显式 few-shot 设计评估，而不是继续
向普通 prompt 塞业务特例。限制每次选择的示例数量，并测试示例选择与顺序、标签/答案
分布、zero-shot 与 few-shot 差异，以及不同模型下的回归。

## 将 hot-only 请求与可复用 Agent 隔离

当可复用且已配置的 Agent 必须创建严格 hot-only 请求时，应使用原生隔离请求边界：

```python
request = agent.create_temp_request()

# 需要其他 create_request(...) 选项时，以下写法等价：
request = agent.create_request(
    inherit_agent_prompt=False,
    inherit_extension_handlers=False,
)
```

这两种写法会关闭 Agent prompt 与 Agent extension handler 的继承；请求仍会使用该
Agent 的请求基础设施和 settings。如果确实要继承，应声明获准继承的槽位与 handler，
并测试这份显式契约，而不是声称请求是 hot-only。

应通过已安装的 runtime，在继承与 extension 注入都有机会运行后，审计最终 post-prefix
ModelRequest prompt。测试必须覆盖请求契约允许的每一种机制，保留证据前还要脱敏。
如果 fake fluent-call 测试只记录 `.input(...)`、`.instruct(...)` 或 `.output(...)`
调用，却没有实现真实 Agent 继承、extension handler 或 prompt prefix，它不能证明隔离。

## 严格的外部接口契约

当模型输出会直接作为已定义 API 请求、模块接口或函数调用的参数时，模型必须
看到该接口契约。Python signature、OpenAPI operation、JSON Schema、protobuf
定义或权威 docstring 不会自动出现在普通模型请求中。

把各槽位组合成一份集成契约：

| 槽位 | 集成职责 |
|---|---|
| `input` | 本次请求的动态值与源事实。 |
| `info` | 权威 API/schema 文档、signature、docstring、字段语义与已声明约束。 |
| `instruct` | 输入如何转换、目标 callable/operation，以及缺失信息如何处理。 |
| `output` | 下游接口要求的精确机器可消费类型与嵌套结构。 |

每个会被下游消费的输出字段都应说明含义，并声明类型、必填性，以及适用的
枚举、格式、范围、可空性或跨字段约束。复用这些权威接口事实属于必要的边界
与输出控制，不是业务逻辑侵入。不属于接口契约的业务决策仍由应用策略层拥有；
真实调用前，host 仍应执行确定性校验。

```python
from typing import Literal

ticket_body = await (
    agent
    .input({
        "request_text": request_text,
        "requester_id": requester_id,
    })
    .info({
        "target_operation": "POST /tickets",
        "operation_contract": openapi_ticket_operation,
    })
    .instruct([
        "根据输入事实生成一份 POST /tickets 请求 body。",
        "严格遵守目标 operation 契约，不要增加字段。",
    ])
    .output({
        "title": (
            str,
            "POST /tickets 接受的非空工单标题。",
            "not_null",
        ),
        "priority": (
            Literal["low", "normal", "high"],
            "必填 API 枚举：low、normal 或 high。",
            True,
        ),
        "requester_id": (
            str,
            "从 input 原样复制的必填请求人标识。",
            "not_null",
        ),
    }, format="json")
    .async_start()
)
```

agent 级持久设置：

```python
agent = (
    Agently.create_agent()
    .role("你是 Agently 客服助手。", always=True)
    .info({"product": "Agently 4.x"}, always=True)
)
```

`always=True` 表示该槽位停留在 agent 级，每次请求都带。

请求级单次设置：

```python
result = (
    agent
    .instruct(["回复不超过 80 字。", "不要编造产品名。"])
    .input("怎么配置一个模型？")
    .output({"answer": (str, "answer", True)})
    .start()
)
```

这里 `instruct(...)` 没传 `always=True`，所以仅本次请求生效。

## Agent vs Execution 作用域

| 作用域 | API |
|---|---|
| Agent definition（每次 execution 都生效） | `.define(...)`、`.role(..., always=True)`、`.info(..., always=True)`、`.set_agent_prompt(key, value)` |
| AgentExecution draft（仅一次 execution 生效） | `.input(...)`、`.output(...)`、`.set_execution_prompt(key, value)` |

同一作用域内最后一次设置覆盖前面，所以可以在单个 execution 里覆盖 agent 默认值，而不修改 agent。

## YAML / JSON prompt 文件

同一套槽位模型，声明式写法：

```yaml
# prompts/triage.yaml
$ensure_all_keys: true
.agent:
  system: 你是一个工单分流助手。
  info:
    severities: ["P0", "P1", "P2", "P3"]
.execution:
  instruct: 对工单文本分类。
  output:
    $format: json
    severity:
      $type: str
      $desc: P0/P1/P2/P3 之一
      $ensure: true
    rationale:
      $type: str
      $desc: 一行说明原因
      $ensure: true
```

加载：

```python
agent = Agently.create_agent().load_yaml_prompt("prompts/triage.yaml")

result = (
    agent
    .create_execution()
    .set_execution_prompt("input", "EU 区域所有用户登录失败。")
    .start()
)
```

`load_json_prompt(...)` 是 JSON 版本的同一 API。两者都接受路径或原始字符串。可以一份配置一个 prompt，也可以用 `prompt_key_path="demo.output_control"` 在多 prompt 文件里挑一个。

Prompt 配置使用 `.execution` 表示单次 execution。turn/request-scoped prompt
config alias 已移除；旧 prompt 文件应改成 `.execution`。

顶层 `$ensure_all_keys: true` 会强制所有叶子都必填，覆盖每叶子的 `$ensure`。整个 schema 必须完整返回时使用。

`output` 块里的 `$format` 会映射到 `.output(..., format=...)` 同一个输出格式设置。
支持 `auto`、`json`、`flat_markdown`、`hybrid`、`xml_field`、`yaml_literal`。如果配置文件需要更明确的 key，
也可以写 `.format`、`$output_format` 或 `.output_format`。

## 往返转换

可以把代码里组装的 prompt 转成 YAML/JSON 用于 review 或存储：

```python
execution = agent.role("你是 Agently 助手。", always=True).input("打个招呼。").output({
    "reply": (str, "reply", True),
})
print(execution.get_yaml_prompt())
print(execution.get_json_prompt())
print(execution.get_prompt_text())  # 用于发送前审计的已渲染 execution draft
```

这种往返用于审查已编写的 execution draft 及其 mappings；若 runtime extension 还会
注入内容，它就不是最终 prompt 的证据。

## 占位符

prompt 槽位中：`{name}` 引用另一个槽位的 key；`${name}` 在加载时由 `mappings={"name": "value"}` 替换。常见用法：

- `instruct: "Reply {input} politely."` — 把请求的 `input` 拉进 instruct。
- `${ENV.OPENAI_API_KEY}` 是**设置**层的环境变量替换，不是 prompt 的；prompt 用 `${name}` + 显式 mappings。
- `${INPUT.customer}`、`${INFO.policy}`、`${INSTRUCT.step}` 是渲染时的 slot
  引用，会变成 `[INPUT > customer]` 这类 prompt 段落指针，而不是把另一个
  slot 的值复制进来。Slot 名大小写不敏感，文档推荐大写。Slot 后面的 path
  不做存在性校验，因为它只是给模型看的引用标签。
- `${OUTPUT}` 是 `[OUTPUT REQUIREMENT]` 的别名。

加载时触发 `${...}` 替换：

```python
agent.load_yaml_prompt(yaml_text, mappings={"product_name": "Agently"})
```

## 每层 prompt 的来源

请求实际发出时，Agently 从以下几层组成模型 prompt：

1. Agent 级槽位（`always=True` 或 `set_agent_prompt`）
2. Request 级槽位（不带 `always=True`）
3. 框架扩展或应用代码填入的槽位（Session 注入 chat history；检索代码通常把片段放进本次请求的 `info(...)`）

一次性链式调用后，用 `execution.get_prompt_text()` 检查发送前已渲染的 execution
draft，例如 `execution = agent.input(...).output(...)`。它不能证明 runtime 的 late
injection 最终加入了什么。当第三层可能改变 prompt 时，应在有界测试中检查注入后的
最终 ModelRequest `prompt_text`，并在保留前脱敏秘密信息。`agent.get_prompt_text()`
只查看保留在 Agent 自身上的 prompt，例如通过 `always=True` 设置的持久槽位。

## 另见

- [Schema as Prompt](schema-as-prompt.md) — 叶子 authoring 与 `$ensure`
- [输出控制](output-control.md) — 解析之后的事
- [项目结构](../start/project-framework.md) — 多 prompt 项目的目录布局
