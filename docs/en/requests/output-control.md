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

`.output(...)` reads its omitted format default from
`prompt.default_output_format`, whose global default is `json`. Agent-level and
request-level settings can override that default independently. Set
`prompt.default_output_format="auto"` only when a target model has passed
representative structured-output stability checks.

Explicit `format="auto"` chooses the structured format from the schema shape:
flat string-only dicts use `xml_field`; dicts that mix string fields with typed
non-string fields use `hybrid`; all-complex, all-control, and non-dict schemas
stay `json`. Auto does not inspect business meaning in field names or
descriptions. If downstream code relies on a specific wire shape, set the format
explicitly. `yaml_literal` is explicit opt-in and is not selected by auto.
`flat_markdown` remains explicit-only for compatibility.

| Mode | Use When | Avoid When |
|---|---|---|
| `auto` | You explicitly accept schema-driven format selection and retry latency for a model that has passed stability checks. Good for application code that consumes parsed data through Agently rather than raw model text. | You need the conservative framework default, a legacy consumer, test fixture, external API, or saved prompt expects raw JSON text. Use `format="json"` or leave the default at `json`. |
| `flat_markdown` | Explicit compatibility mode for legacy section-header prompts. | Auto selection, nested lists/objects, arrays of records, or high-reliability parsing. |
| `hybrid` | Explicit format, or auto target, when string prose/code fields are mixed with typed fields. String fields stay as Markdown sections; list/object/boolean/number fields use fenced JSON blocks. | There are no string prose/code fields, every field is compact machine data where JSON is simpler, the target model echoes section scaffolding, or a downstream consumer cannot tolerate Markdown-section raw output. |
| `xml_field` | Explicit format, or auto target, for flat string-only dict schemas. Agently parses this with a custom XML-like parser, not strict XML. | A downstream consumer expects real XML semantics, namespaces, entity escaping, or schema validation. |
| `yaml_literal` | Explicit opt-in for teams that prefer YAML documents and can tolerate YAML indentation sensitivity. Long text/code fields use YAML literal scalars (`|`) inside `<<<BEGIN AGENTLY_YAML>>>` / `<<<END AGENTLY_YAML>>>` boundaries. | General auto mode, low-adherence models, or dense machine contracts where JSON is simpler and less indentation-sensitive. |
| `json` | You need the strictest machine contract, nested data, arrays, interop with external systems, compatibility with old prompts/tests, or exact raw JSON behavior. | Large embedded documents or code blocks make escaping fragile or hard for the model to read. |
| Plain text | The request asks for one freeform artifact: an article, email, explanation, report, Markdown page, HTML page, or other single multi-paragraph document. Do not call `output()`; use `start()` / `async_start()` directly or read `response.result.get_text()`. | You need separately addressable fields, path validation, `ensure_keys`, typed objects, or downstream branching. |

### Instant Streaming

Use `get_generator(type="instant")` or `get_async_generator(type="instant")`
when the caller benefits from field-level structured updates before the full
response is finished: progress panels, live forms, long reports with
independently renderable sections, model-stage dashboards, or workflow UIs that
can route one field while the rest of the response is still generating. For one
freeform text artifact, use `type="delta"` instead; plain text has no structured
field paths for instant events.

`instant` events are not "final result chunks". They are `StreamingData` patches:

- `path` identifies the field, such as `customer_reply` or `risk_flags[0]`;
- `wildcard_path` normalizes indexes, such as `risk_flags[*]`;
- `delta` is the new fragment for progressive rendering;
- `value` is the parser's current value for that path;
- `is_complete` / `event_type == "done"` marks a field as closed.

Use the stream for provisional UI/progress. Use `get_data()` /
`async_get_data()` after the stream for durable business state; it reads the
cached final parse from the same response and does not issue a second model
request.

| Output Mode | Instant Support | Practical Guidance |
|---|---|---|
| `auto` | Yes, after auto resolves to `json`, `hybrid`, or `xml_field`. | Use only when explicit schema-driven selection is acceptable. If auto later degrades to JSON during final parsing, discard or overwrite provisional UI state with the final parsed result. |
| `flat_markdown` | Yes, field-level text deltas by `### field` sections. | Explicit compatibility mode. Prefer `json` for omitted-format defaults, and use explicit `xml_field` or `hybrid` only when their boundaries fit the target model. |
| `hybrid` | Yes, field-level text deltas by section. JSON block contents stream as text and are parsed into typed values at finalization. | Explicit path for prose/code plus structured records or control fields. Use instant for UI/progress, then use `get_data()` / `async_get_data()` for the finalized typed structure. |
| `xml_field` | Yes, field-level text deltas inside `<field name="..." type="...">` blocks. | Useful when explicit boundaries are easier for the target model than Markdown section headers. Final parsing consumes the normalized answer payload, not provider reasoning. |
| `yaml_literal` | Yes, top-level field deltas inside the target YAML boundary. | Treat as provisional UI state. Final YAML parsing is indentation-sensitive and should be checked through `get_data()`. |
| `json` | Yes, via incremental JSON parsing. | Best when arrays or nested objects need path-level updates. More sensitive to malformed or delayed JSON syntax while streaming; final repair still happens at completion. |
| Plain text / `text` | No structured instant paths. | Use `type="delta"` for raw token streaming, or `get_text()` after completion. |

### Current Format Contracts

Current guidance is based on the implemented parser/prompt contracts and should
be validated against representative target models before broad production
rollout. Experimental runs for format recommendation must store raw outputs and
validate only parsing, required field presence, and structural types. They must
not use tokenization, keywords, or substring matching as the correctness signal
for model-owned content.

| Concern | Contract |
|---|---|
| `auto` selection | Uses schema structure only. It does not inspect field names, descriptions, model output, or business meaning. |
| `flat_markdown` | Explicit compatibility mode only; it is no longer selected by auto. |
| default selection | Omitted `.output(..., format=...)` reads `prompt.default_output_format`; the global default is `json`. |
| `hybrid` | String fields are Markdown sections. Non-string fields are fenced JSON blocks and must parse as JSON values, including booleans and numbers. Explicit `format="hybrid"` or auto can select it for mixed string + typed schemas. Current qwen2.5:7b stability checks found scaffold/header omissions and copied scaffold comments, so keep it explicit unless the target model has passed representative tests. |
| `xml_field` | Uses one `<agently_output>` payload with `<field name="..." type="text|json">` blocks. The parser is XML-like and boundary-based, not strict XML. Explicit `format="xml_field"` or auto can select it for flat string-only dict schemas. |
| `yaml_literal` | Uses a target YAML boundary and literal scalars for long text. It is explicit opt-in and remains outside auto by default. |
| reasoning text | Provider-native reasoning and leading outer `<think>...</think>` content before the payload are normalized to reasoning events before parsing. Payload/code/text-internal `<think>` content is preserved. |

Typical usage:

```python
# Default: json, read from prompt.default_output_format.
agent.input("Create a self-contained page.").output({
    "html": (str, "complete HTML document"),
    "notes": (str, "short implementation notes"),
}).start()

# Per-agent opt-in: omitted .output(..., format=...) now uses auto.
agent.set_settings("prompt.default_output_format", "auto")
agent.input("Create a self-contained page.").output({
    "html": (str, "complete HTML document"),
    "notes": (str, "short implementation notes"),
}).start()

# Force JSON when a downstream contract expects raw JSON-like structure.
agent.input("Extract invoice fields.").output({
    "vendor": (str, "vendor name", True),
    "line_items": [{"sku": (str,), "amount": (float,)}],
}, format="json").start()

# Explicit hybrid when prose/code fields are mixed with records.
agent.input("Create an EDA netlist with design notes.").output({
    "analysis": (str, "one paragraph design rationale", True),
    "components": [{"refdes": (str, "reference designator", True), "value": (str, "part value", True)}],
    "nets": [{"name": (str, "net name", True), "connections": [{"refdes": (str, "refdes", True), "pin": (str, "pin", True)}]}],
}, format="hybrid").start()

# XML-like field envelope for long text mixed with typed records.
agent.input("Create lesson material.").output({
    "lesson_script": (str, "long lesson script", True),
    "environment_checklist": [{"item": (str,), "why": (str,), "command": (str,)}],
    "final_confirmation": (str, "one sentence", True),
}, format="xml_field").start()

# Plain text: one artifact, no structured parser.
html = agent.input("Write a complete landing page as HTML.").start()
```

Progressive UI example:

```python
response = (
    agent
    .input("Turn this incident note into a customer-safe update: ...")
    .output(
        {
            "status_summary": (str, "one sentence status", True),
            "risk_flags": [(str, "risk flag", True)],
            "customer_reply": (str, "customer-safe reply", True),
        },
        format="json",
    )
    .get_response()
)

ui_state = {}

async for item in response.get_async_generator(type="instant"):
    if item.delta:
        ui_state[item.path] = ui_state.get(item.path, "") + item.delta
        await websocket.send_json({
            "path": item.path,
            "delta": item.delta,
            "done": item.is_complete,
        })

final = await response.async_get_data()
await save_case_update(final)
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
