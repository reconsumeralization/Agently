---
title: Output Control
description: The output validation pipeline — strict output, ensure_keys, custom validate, retries, and events.
keywords: Agently, output, validate, ensure_keys, retry, max_retries
---

# Output Control

> Languages: **English** · [中文](../../cn/requests/output-control.md)

The validation pipeline runs the first time a structured response result is consumed, then caches the outcome on that response result. It has a fixed order, and each step contributes to the same retry budget.

For Agently `4.1.0.1+`, the default authoring path is: mark fixed required leaves directly in `.output(...)` with the third-slot `ensure` flag, then let the runtime compile those flags into `ensure_keys`. Pass `ensure_keys=` manually only when the required path is runtime-dependent, conditional, or easier to express outside the static schema.

## Choosing An Output Format

`.output(...)` defaults to `format="auto"`. Auto chooses the simplest structured
format from the schema shape: flat string-only dicts become `flat_markdown`;
dicts that mix string fields with complex list/object fields become `hybrid`;
boolean/numeric control fields, all-complex, and non-dict schemas stay `json`.
Auto does not inspect business meaning in field names or descriptions. If
downstream code relies on a specific wire shape, set the format explicitly.

| Mode | Use When | Avoid When |
|---|---|---|
| `auto` | You want the framework default and the schema itself should choose the most model-friendly structured format. Good for application code that consumes parsed data through Agently rather than the raw model text. | A legacy consumer, test fixture, external API, or saved prompt expects raw JSON text. Use `format="json"` there. |
| `flat_markdown` | The result is a flat dict of named scalar fields, especially when one or more fields may contain large code, HTML, SVG, Markdown, SQL, templates, or multi-paragraph prose. Section headers avoid JSON escaping problems and keep big text blocks readable. | You need nested lists/objects, arrays of records, or a single unstructured document. |
| `hybrid` | The result mixes prose/code fields with structured lists or objects, for example `summary` plus `citations`, `analysis` plus `components`, or `notes` plus `next_steps`. Scalar fields stay as text sections; complex fields use JSON blocks. | Every field is scalar, or every field is deeply structured and compact enough for JSON. |
| `json` | You need the strictest machine contract, nested data, arrays, interop with external systems, compatibility with old prompts/tests, or exact raw JSON behavior. | Large embedded documents or code blocks make escaping fragile or hard for the model to read. |
| Plain text | The request asks for one freeform artifact: an article, email, explanation, report, Markdown page, HTML page, or other single multi-paragraph document. Do not call `output()`; use `start()` / `async_start()` directly or read `response.result.get_text()`. | You need separately addressable fields, path validation, `ensure_keys`, typed objects, or downstream branching. |

### Instant Streaming

Use `get_generator(type="instant")` or `get_async_generator(type="instant")`
when the caller benefits from field-level updates before the full response is
finished: progress panels, live forms, long reports with independently
renderable sections, model-stage dashboards, or workflow UIs that can show a
field as soon as it is complete. For one freeform text artifact, use
`type="delta"` instead; plain text has no structured field paths for instant
events.

| Output Mode | Instant Support | Practical Guidance |
|---|---|---|
| `auto` | Yes, after auto resolves to `json`, `flat_markdown`, or `hybrid`. | Good default for UI streaming when callers consume Agently `StreamingData` rather than raw model text. If auto later degrades to JSON during final parsing, treat instant events as provisional UI data and use the final parsed result for durable writes. |
| `flat_markdown` | Yes, field-level text deltas by `### field` sections. | Strong fit for long scalar fields such as code, HTML, Markdown, or report sections. First update for a field appears after the model emits that field header; pure-numeric scalar schemas have higher header-adherence risk. |
| `hybrid` | Yes, field-level text deltas by section. JSON block contents stream as text and are parsed into lists/objects at finalization. | Best for mixed prose plus structured records. Use instant for UI/progress, then use `get_data()` / `async_get_data()` for the finalized typed structure. |
| `json` | Yes, via incremental JSON parsing. | Best when arrays or nested objects need path-level updates. More sensitive to malformed or delayed JSON syntax while streaming; final repair still happens at completion. |
| Plain text / `text` | No structured instant paths. | Use `type="delta"` for raw token streaming, or `get_text()` after completion. |

### Reliability Notes

The 2026-05-23 cross-model acceptance run covered 6 providers and 12 scenarios
(72 total checks) using DeepSeek, Qwen, Qianfan ERNIE, MiniMax, GLM, and local
Qwen2.5. A follow-up 2026-05-24 structured-output stability smoke run covered
DeepSeek V4 Flash, Qwen3.6-35B-A3B, and GLM-4.5-Air on flat strings, scalar
controls, nested EDA netlists, hybrid EDA netlists, and model-judge arrays.
Treat these as observed compatibility data, not a mathematical guarantee:

| Mode / Scenario Set | Observed Result | Selection Implication |
|---|---|---|
| `auto` overall | The 2026-05-23 run passed 72/72 under the previous broader auto matrix. Current auto is structural: flat string-only dicts may resolve to `flat_markdown`; string-plus-complex dicts may resolve to `hybrid`; boolean/numeric control fields, all-complex, and non-dict schemas resolve to `json`. | Good default when the application consumes final parsed data and can tolerate retry latency, but use explicit format when compatibility matters. |
| `flat_markdown` native parsing | Earlier flat-markdown scenarios showed header adherence risk, especially pure numeric fields and some ERNIE/GLM runs. Current auto avoids booleans/numbers. | Good for large text/code fields; avoid for all-number scalar schemas or models known to ignore section headers. |
| `json` nested structures | In the 2026-05-24 smoke run, nested EDA netlists and nested model-judge arrays passed on DeepSeek V4 Flash, Qwen3.6-35B-A3B, and GLM-4.5-Air except no JSON failure was observed. | Do not avoid complex nested structures categorically. Prefer JSON for dense nested records, judges, booleans, numbers, and machine contracts. |
| `hybrid` nested structures | A prompt-contract gap was found and fixed: complex hybrid sections now include their JSON sub-schema. After the fix, EDA hybrid passed first-attempt on DeepSeek V4 Flash and Qwen3.6-35B-A3B; GLM-4.5-Air hit a 360s request failure with no progress events. | Auto may select hybrid for string fields plus records. Use explicit `json` when a dense nested machine contract is preferred. For reasoning or large MoE models, use a 360s+ timeout and observe streaming/meta events before declaring failure. |
| `instant` sampled scenarios | Instant was included for flat scalar output (S8) and hybrid mixed output (S11) across the provider set. | Supported for UI/progress, but final business decisions should consume the completed parsed result because streaming events are provisional. |

Typical usage:

```python
# Default: auto, chosen from schema shape.
agent.input("Create a self-contained page.").output({
    "html": (str, "complete HTML document"),
    "notes": (str, "short implementation notes"),
}).start()

# Force JSON when a downstream contract expects raw JSON-like structure.
agent.input("Extract invoice fields.").output({
    "vendor": (str, "vendor name", True),
    "line_items": [{"sku": (str,), "amount": (float,)}],
}, format="json").start()

# Explicit hybrid when prose plus records are both useful.
agent.input("Create an EDA netlist with design notes.").output({
    "analysis": (str, "one paragraph design rationale", True),
    "components": [{"refdes": (str, "reference designator", True), "value": (str, "part value", True)}],
    "nets": [{"name": (str, "net name", True), "connections": [{"refdes": (str, "refdes", True), "pin": (str, "pin", True)}]}],
}, format="hybrid").start()

# Plain text: one artifact, no structured parser.
html = agent.input("Write a complete landing page as HTML.").start()
```

## The pipeline

```text
   model returns text
        │
        ▼
1. parse / repair         ← extract structured object from text
        │
        ▼
2. strict output          ← match against .output(...) shape; ensure_all_keys checks if set
        │
        ▼
3. ensure_keys            ← per-leaf required-path checks (compiled from the ensure flag)
        │
        ▼
4. custom validate        ← .validate(handler) and validate_handler= business rules
        │
        ▼
   pass → return result   |   fail → retry (if budget remains) → top of pipeline
```

A failure at any step retries the request. Retries share one budget controlled by `max_retries` (default `3`). When the budget is exhausted:

- `raise_ensure_failure=True` (the default) — raises.
- `raise_ensure_failure=False` — returns the latest parsed result anyway.

## Where validate plugs in

`.validate(handler)` registers a custom check. It runs **after** strict output and `ensure_keys` have already passed, on a canonical dict snapshot of the result.

```python
def must_be_short(result, ctx):
    if len(result.get("answer", "")) > 280:
        return {"ok": False, "reason": "answer too long", "validator_name": "length"}
    return True

agent.input("Summarize.").output({
    "answer": (str, "answer", True),
}).validate(must_be_short).start()
```

The handler runs only on structured-result getters: `start()`, `async_start()`, `get_data()`, `async_get_data()`, `get_data_object()`, `async_get_data_object()`. It does **not** run on `get_text()` / `get_meta()` (those don't carry the parsed structure that validate would inspect).

## Ordered Fields And Evaluation Levels

Agently output schemas are ordered. When later fields depend on earlier
judgment, put support fields first: evidence, assumptions, clarifications,
source notes, calculation plans, concise rationale, rule checks, and
intermediate facts. Put final booleans, verdicts, replies, summaries, and action
decisions last. User-facing renderers can reorder sections for natural reading,
but the model generation contract should keep support-before-conclusion order.

For model-owned grading, confidence, trust, relevance, usability, or quality
judgments, prefer conceptual levels with explicit definitions over precise
numeric scores. For example, ask for `high_trust`, `moderate_trust`, or
`low_trust`, and define each level in the prompt. If downstream code needs a
score for thresholds, weighting, statistics, or index calculations, map levels
to deterministic numbers in code after generation.

For complex arithmetic, long-number calculation, weighting, aggregation, or
statistical transformations, ask the model for an executable calculation plan or
code, run it with tools, and pass the original question, code, and observed
result into the next model step. Do not make text generation be the calculator.

You can also pass handlers per-call:

```python
agent.input("...").output({...}).start(validate_handler=must_be_short)
agent.input("...").output({...}).start(validate_handler=[check_a, check_b])
```

`.validate(...)` handlers run before `validate_handler=` handlers. Multiple `.validate(...)` calls preserve order.

## Handler return shape

| Return | Meaning |
|---|---|
| `True` | pass |
| `False` | fail — retry if budget remains |
| `dict` | structured result; see keys below |

Supported `dict` keys:

| Key | Effect |
|---|---|
| `ok` | `True` = pass, `False` = fail |
| `reason` | text shown in retry events / error messages |
| `payload` | structured details for downstream consumers |
| `validator_name` | tag the validator in events |
| `no_retry` / `stop` | fail but don't retry |
| `error` / `exception` / `raise` | fail with the given exception |

Anything not in this list becomes a `model.validation_error` and consumes retry budget.

## Async handlers

Both sync and async handlers are supported. An async handler signature:

```python
async def check_remote(result, ctx):
    ok = await some_external_check(result["answer"])
    return ok
```

## Context object

The second argument is an `OutputValidateContext` with at least:

- `value`, `input`, `agent_name`, `response_id`
- `attempt_index`, `retry_count`, `max_retries`
- `prompt`, `settings`, `request_run_context`, `model_run_context`
- `response_text`, `raw_text`, `parsed_result`, `result_object`, `typed`, `meta`

Use `ctx.attempt_index` if you want different behavior on later attempts (e.g., loosen the rule on retry).

Treat these fields as observational by default, but `ctx.prompt` and `ctx.settings` are live state objects for the current response-attempt chain. In advanced handlers, if you need to adjust the prompt / options / settings for a **later retry**, you can write back through them inside the validator.

For example, lower sampling parameters on the next retry:

```python
def check(result, ctx):
    if result.get("score", 0) < 0.8 and ctx.retry_count < ctx.max_retries:
        ctx.prompt.set("options", {"temperature": 0.2, "top_p": 0.7})
        return {"ok": False, "reason": "score too low"}
    return True
```

Or change settings:

```python
def check(result, ctx):
    if should_switch_mode(result):
        ctx.settings.set("my_plugin.some_flag", True)
        return False
    return True
```

Two caveats:

- These writes affect **later retries only**. They do not change the current attempt that has already completed.
- These writes also do **not** leak into later fresh requests. Each new `response` is created from a new prompt/settings snapshot at the request/agent layer, so validator write-backs stay inside the current response's retry chain.
- Do not rely on mutating `opts = ctx.prompt.get("options", {})` in place. `get()` returns a view/copy; use write APIs such as `ctx.prompt.set(...)`, `ctx.prompt.update(...)`, or `ctx.settings.set(...)` if you need the change to persist.

## Single execution per response

Validation runs **once** per `ModelResponseResult` and the outcome is cached. Repeated calls — `get_data()` then `get_data()` again, or `get_data()` then `get_data_object()` — do **not** rerun validators. If you try to inject a different handler on the same response after validation has already finalized, the new handler is ignored with a warning.

This means: don't expect to swap validators per consumer. If you need different validation for different consumers, run the request twice.

## Retry events and visibility

Validation contributes two new observation event types:

- `model.validation_failed` — handler returned a fail
- `model.validation_error` — handler raised, returned an unsupported value, etc.

There is intentionally **no** `model.validation_passed` event in phase 1 — passing is the silent default.

The standard `model.retrying` event picks up validation-specific fields when the retry came from validate:

- `retry_reason`, `validator_name`, `validation_reason`, `validation_payload`

Agently-DevTools consumes these defensively. New event keys are additive and should not break existing dashboards.

## Combining with ensure_keys

`ensure_keys` and `.validate(...)` are layered:

- `ensure_keys` handles **path presence** (compiled from the `ensure` flag in `.output(...)`).
- `.validate(...)` handles **value rules** that depend on the actual content.

For fixed required leaves, prefer `(TypeExpr, "description", True)` in `.output(...)` rather than manually repeating the same paths in `ensure_keys=`. Use manual `ensure_keys` for conditional or runtime-only paths. Use `.validate(...)` for "this field must satisfy this business rule".

## Common patterns

**Loosen on the last retry**:

```python
def check(result, ctx):
    if ctx.attempt_index == ctx.max_retries:
        return True  # accept whatever came back
    return strict_check(result)
```

**Fail without retrying** (e.g., validation reveals a permanent business issue):

```python
def policy_check(result, ctx):
    return {"ok": False, "reason": "policy violation", "no_retry": True}
```

**Raise a custom exception**:

```python
def policy_check(result, ctx):
    return {"ok": False, "raise": MyDomainError("rejected by policy")}
```

## See also

- [Schema as Prompt](schema-as-prompt.md) — `.output(...)` authoring and `ensure` flag
- [Model Response](model-response.md) — what cached vs re-runnable means in practice
- [Glossary: ensure](../reference/glossary.md#ensure-third-tuple-slot)
