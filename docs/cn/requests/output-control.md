---
title: 输出控制
description: 输出校验流水线 —— strict output、ensure_keys、custom validate、retry 与事件。
keywords: Agently, output, validate, ensure_keys, retry, max_retries
---

# 输出控制

> 语言：[English](../../en/requests/output-control.md) · **中文**

第一次消费结构化 response 结果时，校验流水线会运行并缓存结果。它的执行顺序固定，每一步都共用同一份 retry 预算。

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

handler 第二个参数是只读的 `OutputValidateContext`，至少包含：

- `value`、`input`、`agent_name`、`response_id`
- `attempt_index`、`retry_count`、`max_retries`
- `prompt`、`settings`、`request_run_context`、`model_run_context`
- `response_text`、`raw_text`、`parsed_result`、`result_object`、`typed`、`meta`

需要根据「第几次尝试」改变行为时（如最后一次放宽规则），用 `ctx.attempt_index`。

## 单 response 单次执行

每个 `ModelResponseResult` 只跑**一次** validation 并缓存结果。多次调用——`get_data()` 再 `get_data()`，或 `get_data()` 后 `get_data_object()`——**不会**重跑 validator。如果 validation 已经定型后再注入新 handler，新 handler 被忽略并发 warning。

含义：不要为不同 consumer 切换 validator。需要不同校验时，发两次请求。

## Retry 事件与可观测

validate 引入两个新 runtime event：

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

「这字段必须出现」用 `ensure_keys`。「这字段必须满足某业务规则」用 `.validate(...)`。

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
