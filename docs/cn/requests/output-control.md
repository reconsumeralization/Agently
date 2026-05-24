---
title: 输出控制
description: 输出校验流水线 —— strict output、ensure_keys、custom validate、retry 与事件。
keywords: Agently, output, validate, ensure_keys, retry, max_retries
---

# 输出控制

> 语言：[English](../../en/requests/output-control.md) · **中文**

第一次消费结构化 response 结果时，校验流水线会运行并缓存结果。它的执行顺序固定，每一步都共用同一份 retry 预算。

对 Agently `4.1.0.1+`，默认 authoring 路径是：在 `.output(...)` 里直接用第三槽 `ensure` 标记固定必填叶子，再由运行时把这些标记编译成 `ensure_keys`。只有当必填路径是运行时决定、条件分支决定，或用静态 schema 不好表达时，才手动传 `ensure_keys=`。

## 选择输出格式

`.output(...)` 默认是 `format="auto"`。Auto 会根据 schema 形态选择最简单的
结构化格式：扁平纯字符串 dict 走 `flat_markdown`；字符串字段与复杂
list/object 字段混合的 dict 走 `hybrid`；布尔/数字控制字段、全复杂结构或非
dict 输出仍走 `json`。Auto 不检查字段名或描述里的业务含义。如果下游代码依赖
固定的原始输出形态，应显式指定格式。

| 模式 | 适用场景 | 不适合 |
|---|---|---|
| `auto` | 希望使用框架默认策略，并让 schema 自己决定最适合模型生成的结构化格式。适合应用代码通过 Agently 消费解析后的数据，而不是依赖模型原始文本。 | 旧消费者、测试 fixture、外部 API 或保存的 prompt 期待原始 JSON 文本。此时显式用 `format="json"`。 |
| `flat_markdown` | 输出是扁平 dict，字段都是标量，尤其某些字段可能包含大量代码、HTML、SVG、Markdown、SQL、模板或多段 prose。用章节标题隔离字段，可以减少 JSON 转义问题，也让大文本块更易读。 | 需要嵌套 list/object、记录数组，或只需要一个非结构化文档。 |
| `hybrid` | 输出同时有 prose/code 字段和结构化 list/object，例如 `summary` + `citations`、`analysis` + `components`、`notes` + `next_steps`。标量字段保持文本章节，复杂字段放 JSON code block。 | 所有字段都是标量，或所有字段都是紧凑的深层结构且 JSON 更直接。 |
| `json` | 需要最稳定的机器契约、嵌套数据、数组、外部系统互通、兼容旧 prompt/测试，或下游明确依赖原始 JSON 行为。 | 大段嵌入文档或代码会让转义变脆弱，也更难让模型稳定生成。 |
| 纯文本 | 请求只要一个自由文本成品：文章、邮件、解释、报告、Markdown 页面、HTML 页面，或其他单一多段落文档。不要调用 `output()`；直接用 `start()` / `async_start()`，或读取 `response.result.get_text()`。 | 需要可单独寻址的字段、路径校验、`ensure_keys`、typed object 或下游分支。 |

### Instant Streaming

当调用方需要在完整响应结束前看到字段级更新时，使用
`get_generator(type="instant")` 或 `get_async_generator(type="instant")`：
进度面板、实时表单、可分区渲染的长报告、模型阶段 dashboard，或能在字段完成后
立即展示的 workflow UI。对于单一自由文本成品，用 `type="delta"`；纯文本没有
结构化字段路径可供 instant 事件使用。

| 输出模式 | Instant 支持 | 使用建议 |
|---|---|---|
| `auto` | 支持，auto 先解析为 `json`、`flat_markdown` 或 `hybrid` 后使用对应流式解析器。 | UI streaming 的默认选择，前提是调用方消费 Agently 的 `StreamingData`，而不是依赖模型原始文本。如果 auto 最终降级到 JSON 重试，把 instant 事件当作临时 UI 数据，持久写入以最终解析结果为准。 |
| `flat_markdown` | 支持，按 `### field` 章节输出字段级 text delta。 | 适合代码、HTML、Markdown、报告章节等长标量字段。字段的首个更新会在模型输出该字段 header 后出现；纯数字标量 schema 的 header 遵循风险更高。 |
| `hybrid` | 支持，按章节输出字段级 text delta。JSON block 内容先按文本流出，最终再解析成 list/object。 | 适合 prose + 结构化 records 的混合输出。instant 用于 UI/进度，最终 typed 结构用 `get_data()` / `async_get_data()`。 |
| `json` | 支持，走增量 JSON parser。 | 适合数组或嵌套对象的路径级更新。流式阶段更依赖模型及时输出合法 JSON 片段；完成后仍会做最终 repair/parse。 |
| 纯文本 / `text` | 不提供结构化 instant path。 | 用 `type="delta"` 做原始 token 流式，或完成后 `get_text()`。 |

### 可靠性记录

2026-05-23 的 cross-model 验收覆盖了 6 个 provider、12 个场景（共 72 个
检查），包括 DeepSeek、Qwen、Qianfan ERNIE、MiniMax、GLM 和本地 Qwen2.5。
2026-05-24 追加的结构化输出稳定性 smoke run 覆盖了 DeepSeek V4 Flash、
Qwen3.6-35B-A3B、GLM-4.5-Air，场景包括扁平字符串、标量控制字段、嵌套
EDA netlist、hybrid EDA netlist 和 model-judge 数组。这些是已观察到的兼容性
数据，不是数学保证：

| 模式 / 场景集合 | 观察结果 | 选择含义 |
|---|---|---|
| `auto` overall | 2026-05-23 旧的 broader auto 矩阵 72/72 通过。当前 auto 是结构规则：扁平纯字符串 dict 可走 `flat_markdown`；字符串字段 + 复杂字段的 dict 可走 `hybrid`；布尔/数字控制字段、全复杂结构和非 dict 输出走 `json`。 | 应用消费最终解析数据、且可接受重试延迟时，适合作为默认；需要兼容固定线协议时显式指定格式。 |
| `flat_markdown` native parsing | 早期 flat_markdown 场景暴露过 header 遵循风险，尤其纯数字字段以及部分 ERNIE/GLM 运行。当前 auto 不再把数字/布尔字段放进 flat_markdown。 | 适合大段文本/代码字段；不适合全数字标量 schema 或已知不稳定遵循章节标题的模型。 |
| `json` 嵌套结构 | 2026-05-24 smoke run 中，嵌套 EDA netlist 和嵌套 model-judge 数组在 DeepSeek V4 Flash、Qwen3.6-35B-A3B、GLM-4.5-Air 上未观察到 JSON 输出失败。 | 不要一刀切禁用复杂嵌套结构。dense nested records、judge、布尔、数字和机器契约优先用 JSON。 |
| `hybrid` 嵌套结构 | 实验中发现并修复了一个 prompt contract 缺口：复杂 hybrid section 现在会包含自己的 JSON 子结构。修复后 EDA hybrid 在 DeepSeek V4 Flash 和 Qwen3.6-35B-A3B 上首轮通过；GLM-4.5-Air 在该 hybrid 场景 360s 无 progress request failure。 | Auto 可为字符串字段 + records 选择 hybrid；如果业务更希望 dense nested machine contract，显式指定 `json`。reasoning 或大型 MoE 档位建议 360s+ timeout，并监听 streaming/meta 事件后再判断失败。 |
| `instant` sampled scenarios | Instant 覆盖了 flat scalar 输出（S8）和 hybrid 混合输出（S11），并在 provider 集合中验收。 | 支持 UI/进度展示，但业务决策与持久化应消费完成后的最终解析结果；streaming event 是临时状态。 |

典型用法：

```python
# 默认：auto，根据 schema 形态选择格式。
agent.input("Create a self-contained page.").output({
    "html": (str, "complete HTML document"),
    "notes": (str, "short implementation notes"),
}).start()

# 下游契约期待 JSON 时，显式固定 json。
agent.input("Extract invoice fields.").output({
    "vendor": (str, "vendor name", True),
    "line_items": [{"sku": (str,), "amount": (float,)}],
}, format="json").start()

# prose 加结构化 records 都有价值时，显式使用 hybrid。
agent.input("Create an EDA netlist with design notes.").output({
    "analysis": (str, "one paragraph design rationale", True),
    "components": [{"refdes": (str, "reference designator", True), "value": (str, "part value", True)}],
    "nets": [{"name": (str, "net name", True), "connections": [{"refdes": (str, "refdes", True), "pin": (str, "pin", True)}]}],
}, format="hybrid").start()

# 纯文本：一个成品文档，不走结构化 parser。
html = agent.input("Write a complete landing page as HTML.").start()
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

每个 `ModelResponseResult` 只跑**一次** validation 并缓存结果。多次调用——`get_data()` 再 `get_data()`，或 `get_data()` 后 `get_data_object()`——**不会**重跑 validator。如果 validation 已经定型后再注入新 handler，新 handler 被忽略并发 warning。

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
