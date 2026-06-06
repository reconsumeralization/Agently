---
title: 输出控制
description: 输出校验流水线 —— strict output、ensure_keys、custom validate、retry 与事件。
keywords: Agently, output, validate, ensure_keys, retry, max_retries
---

# 输出控制

> 语言：[English](../../en/requests/output-control.md) · **中文**

第一次消费结构化 response 结果时，校验流水线会运行并缓存结果。它的执行顺序固定，每一步都共用同一份 retry 预算。

对 Agently `4.1.0.1+`，默认 authoring 路径是：在 `.output(...)` 里直接用第三槽 `ensure` 标记固定必填叶子，再由运行时把这些标记编译成 `ensure_keys`。只有当必填路径是运行时决定、条件分支决定，或用静态 schema 不好表达时，才手动传 `ensure_keys=`。必填字符串叶子必须是非空白文本；缺 key、`None`、空白字符串、空 wildcard 结果，或 wildcard 结果里包含空白必填值，都会进入同一套 retry 流程。`False` 和 `0` 仍然是有效必填值。

## 选择输出格式

`.output(...)` 省略 `format` 时读取 `prompt.default_output_format`，全局默认值是
`json`。agent 级和 request 级 settings 可以独立覆盖这个默认值。只有目标模型通过
代表性结构化输出稳定性测试后，才建议把 `prompt.default_output_format` 设为
`"auto"`。

显式 `format="auto"` 时，Auto 会根据 schema 形态选择结构化格式：扁平纯字符串
dict 走 `xml_field`；字符串字段与 typed 非字符串字段混合的 dict 走 `hybrid`；
全复杂、全控制字段或非 dict 输出仍走 `json`。Auto 不检查字段名或描述里的业务含义。
如果下游代码依赖固定的原始输出形态，应显式指定格式。`yaml_literal` 是显式
opt-in 格式，不进入 auto；`flat_markdown` 仅作为显式兼容模式保留。

| 模式 | 适用场景 | 不适合 |
|---|---|---|
| `auto` | 明确接受 schema-driven 格式选择和重试延迟，并且目标模型已经通过稳定性测试。适合应用代码通过 Agently 消费解析后的数据，而不是依赖模型原始文本。 | 需要保守框架默认值，或旧消费者、测试 fixture、外部 API、保存的 prompt 期待原始 JSON 文本。此时显式用 `format="json"` 或保持默认 `json`。 |
| `flat_markdown` | 兼容旧 section-header prompt 的显式模式。 | auto 选择、嵌套 list/object、记录数组，或需要高可靠解析。 |
| `hybrid` | 显式格式，或 auto 目标；适合字符串 prose/code 字段与 typed 字段混合。字符串字段保持 Markdown 章节，list/object/boolean/number 字段放 fenced JSON block。 | 没有字符串 prose/code 字段、所有字段都是紧凑机器数据且 JSON 更直接、目标模型容易回显脚手架，或下游不能接受 Markdown-section raw output。 |
| `xml_field` | 显式格式，或 auto 目标；适合扁平纯字符串 dict。Agently 用自定义 XML-like parser 解析，不是严格 XML parser；text 字段可包含 Markdown、代码、`&` 或类似 XML 的片段。 | 下游消费者期待真实 XML 语义、namespace、entity escaping 或 XML schema validation。 |
| `yaml_literal` | 团队明确偏好 YAML document，且可接受 YAML 缩进敏感性时显式使用。长文本/代码字段用 YAML literal scalar（`|`），整体包在 `<<<BEGIN AGENTLY_YAML>>>` / `<<<END AGENTLY_YAML>>>` boundary 中。 | 通用 auto、低遵循模型，或 JSON 更简单稳定的 dense machine contract。 |
| `json` | 需要最稳定的机器契约、嵌套数据、数组、外部系统互通、兼容旧 prompt/测试，或下游明确依赖原始 JSON 行为。 | 大段嵌入文档或代码会让转义变脆弱，也更难让模型稳定生成。 |
| 纯文本 | 请求只要一个自由文本成品：文章、邮件、解释、报告、Markdown 页面、HTML 页面，或其他单一多段落文档。不要调用 `output()`；直接用 `start()` / `async_start()`，或读取 `result.get_text()`。 | 需要可单独寻址的字段、路径校验、`ensure_keys`、typed object 或下游分支。 |

### Instant Streaming

当调用方需要在完整响应结束前看到字段级更新时，使用
`get_generator(type="instant")` 或 `get_async_generator(type="instant")`：
进度面板、实时表单、可分区渲染的长报告、模型阶段 dashboard，或能在剩余响应还在
生成时先路由某个字段的 workflow UI。对于单一自由文本成品，用 `type="delta"`；
纯文本没有结构化字段路径可供 instant 事件使用。

`instant` 事件不是“最终结果分块”。它是 `StreamingData` patch：

- `path` 标识字段，例如 `customer_reply` 或 `risk_flags[0]`；
- `wildcard_path` 归一化数组下标，例如 `risk_flags[*]`；
- `delta` 是这次新增的片段，用于渐进渲染；
- `value` 是该 path 当前的 parser 值；
- `is_completed` / `event_type == "done"` 表示字段关闭。

把 stream 当作临时 UI / 进度状态。结束后用 `get_data()` / `async_get_data()`
读取可靠业务状态；它读取同一个 response 的最终缓存解析结果，不会重新发模型请求。

| 输出模式 | Instant 支持 | 使用建议 |
|---|---|---|
| `auto` | 支持，auto 先解析为 `json`、`hybrid` 或 `xml_field` 后使用对应流式解析器。 | 仅在明确接受 schema-driven 选择时使用。如果 auto 最终降级到 JSON 重试，用最终解析结果覆盖或丢弃临时 UI 状态。 |
| `flat_markdown` | 支持，按 `### field` 章节输出字段级 text delta。 | 显式兼容模式。省略格式时优先保持 `json` 默认；只有目标模型适配时才显式使用 `xml_field` 或 `hybrid`。 |
| `hybrid` | 支持，按章节输出字段级 text delta。JSON block 内容先按文本流出，最终再解析成 typed 值。 | prose/code + 结构化 records/control fields 的显式路径。instant 用于 UI/进度，最终 typed 结构用 `get_data()` / `async_get_data()`。 |
| `xml_field` | 支持，在 `<field name="..." type="...">` block 内输出字段级 text delta。 | 当显式 boundary 比 Markdown header 更容易被目标模型遵循时使用。最终解析消费归一化后的 answer payload，不消费 provider reasoning。 |
| `yaml_literal` | 支持，在目标 YAML boundary 内输出顶层字段 delta。 | 作为临时 UI 状态使用。最终 YAML parsing 对缩进敏感，应以 `get_data()` 结果为准。 |
| `json` | 支持，走增量 JSON parser。 | 适合数组或嵌套对象的路径级更新。流式阶段更依赖模型及时输出合法 JSON 片段；完成后仍会做最终 repair/parse。 |
| 纯文本 / `text` | 不提供结构化 instant path。 | 用 `type="delta"` 做原始 token 流式，或完成后 `get_text()`。 |

### 当前格式契约

当前指导基于已经实现的 parser / prompt 契约。大规模生产推荐前，应使用代表性目标模型
重新验证。格式推荐实验必须保存原始输出，只校验解析、必填字段存在和结构类型；不得用
分词、关键词或子串匹配作为模型生成内容正确性的判断信号。

| 关注点 | 契约 |
|---|---|
| `auto` 选择 | 只看 schema 结构。不看字段名、描述、模型输出或业务语义。 |
| `flat_markdown` | 仅保留为显式兼容模式，不再由 auto 选择。 |
| 默认选择 | 省略 `.output(..., format=...)` 时读取 `prompt.default_output_format`；全局默认是 `json`。 |
| `hybrid` | 字符串字段是 Markdown section。非字符串字段是 fenced JSON block，并且必须解析成 JSON value，包括 boolean 和 number。显式 `format="hybrid"` 或 auto 会将字符串 + typed 混合 schema 解析到该格式。当前 qwen2.5:7b 稳定性检查发现过标题缺失和脚手架注释回显，因此除非目标模型已通过代表性测试，否则保持显式使用。 |
| `xml_field` | 使用一个 `<agently_output>` payload 和 `<field name="..." type="text|json">` block。parser 是 XML-like boundary parser，不是严格 XML。显式 `format="xml_field"` 或 auto 会将扁平纯字符串 dict 解析到该格式。 |
| `yaml_literal` | 使用目标 YAML boundary；长文本字段使用 literal scalar。显式 opt-in，默认不进入 auto。 |
| reasoning 文本 | provider-native reasoning 和目标 payload 前面的完整外层 `<think>...</think>` 会在解析前归一为 reasoning event。payload/code/text 内部的 `<think>` 会保留。 |
| 元组 `ensure` | 第三槽 `True` 会编译为 `ensure_keys`。对应路径必须解析到可用值：字符串叶子必须非空白，wildcard 匹配结果必须非空且每个必填值可用；`False` 或 `0` 这类 typed 值仍然有效。 |

典型用法：

```python
# 默认：json，来自 prompt.default_output_format。
agent.input("Create a self-contained page.").output({
    "html": (str, "complete HTML document"),
    "notes": (str, "short implementation notes"),
}).start()

# 按 agent 显式 opt-in：省略 .output(..., format=...) 时改用 auto。
agent.set_settings("prompt.default_output_format", "auto")
agent.input("Create a self-contained page.").output({
    "html": (str, "complete HTML document"),
    "notes": (str, "short implementation notes"),
}).start()

# 下游契约期待 JSON 时，显式固定 json。
agent.input("Extract invoice fields.").output({
    "vendor": (str, "vendor name", True),
    "line_items": [{"sku": (str,), "amount": (float,)}],
}, format="json").start()

# prose/code 字段混合 records 时，可显式使用 hybrid。
agent.input("Create an EDA netlist with design notes.").output({
    "analysis": (str, "one paragraph design rationale", True),
    "components": [{"refdes": (str, "reference designator", True), "value": (str, "part value", True)}],
    "nets": [{"name": (str, "net name", True), "connections": [{"refdes": (str, "refdes", True), "pin": (str, "pin", True)}]}],
}, format="hybrid").start()

# 长文本混合 typed records 时，使用 XML-like field envelope。
agent.input("Create lesson material.").output({
    "lesson_script": (str, "long lesson script", True),
    "environment_checklist": [{"item": (str,), "why": (str,), "command": (str,)}],
    "final_confirmation": (str, "one sentence", True),
}, format="xml_field").start()

# 纯文本：一个成品文档，不走结构化 parser。
html = agent.input("Write a complete landing page as HTML.").start()
```

渐进式 UI 示例：

```python
result = (
    agent
    .input("把这条事故记录改写成客户可读状态更新：...")
    .output(
        {
            "status_summary": (str, "一句话状态", True),
            "risk_flags": [(str, "风险点", True)],
            "customer_reply": (str, "客户回复", True),
        },
        format="json",
    )
    .get_result()
)

ui_state = {}

async for item in result.get_async_generator(type="instant"):
    if item.delta:
        ui_state[item.path] = ui_state.get(item.path, "") + item.delta
        await websocket.send_json({
            "path": item.path,
            "delta": item.delta,
            "done": item.is_completed,
        })

final = await result.async_get_data()
await save_case_update(final)
```

## 流水线

```text
   模型返回文本
       │
       ▼
1. parse / repair          ← 从文本中抽取结构化对象
       │
       ▼
2. strict output           ← 对照 .output(...) 形态校验；启用了 ensure_all_keys 则全检查
       │
       ▼
3. ensure_keys             ← 每叶子的必填路径检查（由 ensure 标记编译而来）
       │
       ▼
4. custom validate         ← .validate(handler) 与 validate_handler= 业务规则
       │
       ▼
   通过 → 返回结果   |   失败 → retry（预算未耗尽时）→ 回到顶部
```

任意一步失败都触发重试。重试共用一份预算，由 `max_retries`（默认 `3`）控制。预算耗尽时：

- `raise_ensure_failure=True`（默认）—— 抛异常。
- `raise_ensure_failure=False` —— 直接返回最近一次解析结果。

## validate 在哪一步

`.validate(handler)` 注册自定义检查。它在 strict output 与 `ensure_keys` 都通过**之后**跑，作用对象是结果的 canonical dict snapshot。

```python
def must_be_short(result, ctx):
    if len(result.get("answer", "")) > 280:
        return {"ok": False, "reason": "answer 太长", "validator_name": "length"}
    return True

agent.input("总结。").output({
    "answer": (str, "answer", True),
}).validate(must_be_short).start()
```

handler **只**挂在结构化结果 getter 上：`start()`、`async_start()`、`get_data()`、`async_get_data()`、`get_data_object()`、`async_get_data_object()`。**不挂**在 `get_text()` / `get_meta()` 上（它们没有 validate 要看的解析结构）。

## 字段顺序与评估等级

Agently output schema 是有序的。当后续字段依赖前置判断时，把支撑字段放在前面：
证据、假设、澄清、来源说明、计算计划、简要依据、规则检查、中间事实。最终布尔值、
评判、回复、总结和行动决策放在后面。面向人类展示时可以按自然阅读习惯重排，但模型
生成契约应保持「支撑信息先于结论」。

模型负责分级、置信度、可信度、相关性、可用性或质量评估时，优先使用带明确定义的
概念等级，而不是精确数字分数。例如要求输出 `high_trust`、`moderate_trust`、
`low_trust`，并在提示词里定义每个等级。若下游代码需要阈值、加权、统计或指数化
计算，在模型输出后用代码把等级映射为确定数字。

复杂算术、长位数计算、加权聚合或统计转换不要直接交给模型文本生成。让模型输出可执行
的计算计划或代码，通过工具运行，再把原始问题、代码和运行结果交给后续模型步骤使用。

也可以在调用时传 handler：

```python
agent.input("...").output({...}).start(validate_handler=must_be_short)
agent.input("...").output({...}).start(validate_handler=[check_a, check_b])
```

`.validate(...)` 注册的 handler 先于 `validate_handler=` 传入的。多次 `.validate(...)` 调用顺序保留。

## handler 返回值

| 返回 | 含义 |
|---|---|
| `True` | 通过 |
| `False` | 失败 —— 预算未耗尽则重试 |
| `dict` | 结构化结果，见下表 |

支持的 dict key：

| Key | 效果 |
|---|---|
| `ok` | `True` 通过，`False` 失败 |
| `reason` | 出现在 retry event / 错误信息中 |
| `payload` | 给下游的结构化细节 |
| `validator_name` | 给该 validator 起名（用于事件） |
| `no_retry` / `stop` | 失败但不重试 |
| `error` / `exception` / `raise` | 用指定异常失败 |

不在此列的返回会变成 `model.validation_error` 并消耗预算。

## Async handler

sync 与 async handler 都支持：

```python
async def check_remote(result, ctx):
    ok = await some_external_check(result["answer"])
    return ok
```

## Context 对象

handler 第二个参数是 `OutputValidateContext`，至少包含：

- `value`、`input`、`agent_name`、`response_id`
- `attempt_index`、`retry_count`、`max_retries`
- `prompt`、`settings`、`request_run_context`、`model_run_context`
- `response_text`、`raw_text`、`parsed_result`、`result_object`、`typed`、`meta`

需要根据「第几次尝试」改变行为时（如最后一次放宽规则），用 `ctx.attempt_index`。

默认把这些字段当作观察上下文来读；但 `ctx.prompt` 与 `ctx.settings` 是当前 response attempt 链路上的 live state。高级用法里，如果你要调整**后续 retry** 的 prompt / options / settings，可以在 validator 里直接写回它们。

例如，降低下一次 retry 的采样参数：

```python
def check(result, ctx):
    if result.get("score", 0) < 0.8 and ctx.retry_count < ctx.max_retries:
        ctx.prompt.set("options", {"temperature": 0.2, "top_p": 0.7})
        return {"ok": False, "reason": "score too low"}
    return True
```

或者改 settings：

```python
def check(result, ctx):
    if should_switch_mode(result):
        ctx.settings.set("my_plugin.some_flag", True)
        return False
    return True
```

注意两点：

- 这些写入只影响**后续 retry**，不会改变当前这次已经完成的 attempt。
- 这些写入也**不会污染后续新请求**。每次新建 `response` 时都会从 request / agent 层重新做一次 prompt 与 settings 快照；validator 里的写回只停留在当前 response 的 retry 链里。
- 不要依赖 `opts = ctx.prompt.get("options", {})` 后再原地改 `opts`。`get()` 返回的是 view/copy；要持久生效，使用 `ctx.prompt.set(...)`、`ctx.prompt.update(...)`、`ctx.settings.set(...)` 这类写接口。

## 单 response 单次执行

每个 `ModelResponseResult` 只跑**一次** validation 并缓存结果。多次调用——`get_data()` 再 `get_data()`，或 `get_data()` 后 `get_data_object()`——**不会**重跑 validator。如果 validation 已经定型后再往同一个 result 注入新 handler，新 handler 被忽略并发 warning。

含义：不要为不同 consumer 切换 validator。需要不同校验时，发两次请求。

## Retry 事件与可观测

validate 引入两个新 observation event：

- `model.validation_failed` —— handler 返回失败
- `model.validation_error` —— handler 抛异常 / 返回不支持的值

phase 1 **没有** `model.validation_passed` 事件 —— 通过是默认且静默的。

`model.retrying` 事件在 retry 由 validate 触发时会带上 validation 相关字段：

- `retry_reason`、`validator_name`、`validation_reason`、`validation_payload`

`../Agently-Devtools` 防御性消费这些事件，新 key 不破坏现有 dashboard。

## 与 ensure_keys 的关系

`ensure_keys` 与 `.validate(...)` 是分层的：

- `ensure_keys` 处理**路径存在性**（由 `.output(...)` 中的 `ensure` 编译而来）。
- `.validate(...)` 处理基于实际内容的**值规则**。

固定必填叶子优先写 `(TypeExpr, "description", True)`，不要把同一批路径再手动重复到 `ensure_keys=`。条件型或运行时决定的路径，再用手动 `ensure_keys`。而「这字段必须满足某业务规则」用 `.validate(...)`。

## 常见模式

**最后一次放宽**：

```python
def check(result, ctx):
    if ctx.attempt_index == ctx.max_retries:
        return True  # 接受现有结果
    return strict_check(result)
```

**失败但不重试**（如 validation 暴露了一条永久性业务问题）：

```python
def policy_check(result, ctx):
    return {"ok": False, "reason": "policy violation", "no_retry": True}
```

**抛自定义异常**：

```python
def policy_check(result, ctx):
    return {"ok": False, "raise": MyDomainError("rejected by policy")}
```

## 另见

- [Schema as Prompt](schema-as-prompt.md) —— `.output(...)` authoring 与 `ensure` 标记
- [模型响应](model-response.md) —— 缓存与重跑的实际差别
- [术语表：ensure](../reference/glossary.md#ensure第三槽)
