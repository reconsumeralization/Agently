---
title: Prompt Management
description: Layered prompt slots, agent vs request scope, YAML/JSON loading, and placeholders.
keywords: Agently, prompt, role, system, info, instruct, input, configure_prompt
---

# Prompt Management

> Languages: **English** · [中文](../../cn/requests/prompt-management.md)

Agently splits a prompt into named slots. The slots compose, so you can set persistent agent context once and only fill request-specific slots per call.

## Slot map

| Slot | Where it ends up | Typical use |
|---|---|---|
| `role` / `system` | system message | persona, capability boundaries |
| `info` | system or user (impl detail) | background facts, inventories, tool catalogs |
| `instruct` | user message | step-by-step instructions for this kind of request |
| `input` | user message | the actual question or payload |
| `output` | user message + parser | the schema you want back |

## Keep one request contract local

For a one-off Agently fluent request, keep the request expression visible as
one readable chain: `input`, authoritative `info`, `instruct`, `output` schema,
and the terminal result call such as `get_result()`, `get_data()`, or
`async_get_data()`.

```python
result = (
    agent
    .input({"ticket_text": ticket_text})
    .info({"allowed_queues": allowed_queues})
    .instruct("Select the best queue and give one concise explanation.")
    .output({
        "queue": (str, "One value from allowed_queues.", True),
        "explanation": (str, "Concise user-visible explanation.", True),
    })
    .get_result()
)
triage = await result.async_get_data()
```

A single YAML/JSON Prompt Configure file loaded with explicit `mappings` is the
declarative equivalent. Split the chain only when a piece is reused unchanged,
independently owned/versioned or product-edited, or genuinely generated or
conditional. Do not move a one-use schema or prompt step elsewhere merely to
make the Agently request chain look shorter.

## Request-local context

Every model-visible prompt item must serve at least one current-request role:

1. interpret a supplied input;
2. provide an authoritative fact, policy, schema, or evidence item;
3. change the model-owned decision or transformation;
4. define an output, consumer, tool, or capability boundary;
5. provide useful user-visible process context, state, or explanation with a
   declared user or UI consumer.

Use the prompt slots deliberately: `agent` supplies stable role and
capabilities; `input` supplies current facts; `info` supplies authoritative
contract and evidence; `instruct` supplies task rules; and `output` supplies
the required result shape. Together, they must give the model a self-contained
account of the current request rather than assuming unexplained project
context.

Apply the removal counterfactual to every candidate item: if removing it would
not change the current request's effective task, contract, evidence, decision,
allowed verdict, or declared user/UI projection, remove or rewrite it.
Project-level origin is not a removal test: retain a shared policy or fact when
it changes this request. Retain or behaviorally rewrite an effective upstream
caller guarantee when it changes the model-owned decision or the allowed
verdict set. A proper name may remain only when it identifies a real domain
contract, allowlist, evidence item, input fact, or capability boundary that
changes the current request. Otherwise, rewrite an unexplained implementation
name as its request-relevant role, or remove it. The fifth role does not permit
generic project narration: declare which user or UI consumer uses the process
context, state, or explanation.

| | `info` |
|---|---|
| Bad | “Follow the project’s worker-manager convention.” |
| Good | “Allowed actions: approve or reject. Evidence: the attached request and its policy record.” |

Audit at two levels: first review each slot against its role and the removal
counterfactual; then inspect the rendered request, including mappings and
references. Before dispatch, `execution.get_prompt_text()` audits the rendered
execution draft, not the final ModelRequest prompt. When TaskContext, Session,
Skills, retrieval, Actions, or other runtime extensions can inject later, use a
bounded test to inspect the final ModelRequest `prompt_text` emitted or built
after injection, for example the `prompt.built` event's
`payload.prompt_text`. Do not treat the post-start execution snapshot as
sufficient evidence for late injections. Redact secrets before retaining
prompt evidence.

## Keep special cases out of normative instructions

Do not encode behavior for one customer, component/model name, page state,
incident, fixture, or known answer as a normative prompt branch merely because
that case exposed a failure. First identify the general invariant or missing
decision boundary, state the smallest general rule, and verify it against the
original case plus contrasting valid, invalid, and boundary cases. Remove
incident-specific literals unless the current request supplies them as real
facts.

This does not remove legitimate business context. A current authoritative
business policy, domain invariant, authorization rule, interface contract, or
runtime fact that changes the request still belongs in `info`, `instruct`,
`input`, or `output` according to its owner.

Examples are non-normative. They may clarify an already stated rule, but cannot
introduce behavior, priority, an exception, or an expected answer that the
general contract never states. Mark them clearly, prefer generic or synthetic
content, and use contrasting examples when one example would imply a false
default.

As an Agently prompt-review rule, total illustrative-example content in the
final rendered prompt must remain smaller than the non-example normative prompt
text. Measure both sides consistently by characters or model tokens. This is an
authoring guard, not a claim that model attention has a universal 50 percent
threshold.

If a task appears to need more demonstrations, treat it as an explicit
few-shot design instead of hiding more special cases in the ordinary prompt.
Keep selected demonstrations bounded and test example selection and order,
label/answer balance, zero-shot versus few-shot behavior, and model-specific
regressions.

## Isolate a hot-only request from a reusable Agent

If a reusable configured Agent must create a strict hot-only request, use a
native isolated request boundary:

```python
request = agent.create_temp_request()

# Equivalent when other create_request(...) options are needed:
request = agent.create_request(
    inherit_agent_prompt=False,
    inherit_extension_handlers=False,
)
```

These calls disable inheritance of the Agent prompt and Agent extension
handlers; they still use the Agent's request infrastructure and settings. If
inheritance is intentional, declare the approved inherited slots and handlers,
then test that explicit contract instead of claiming the request is hot-only.

Use the installed runtime to audit the final post-prefix ModelRequest prompt
after inheritance and extension injection have had their opportunity to run.
Cover every mechanism allowed by the request contract and redact retained
evidence. A fake fluent-call test that only records `.input(...)`,
`.instruct(...)`, or `.output(...)` calls cannot prove isolation when it does
not implement real Agent inheritance, extension handling, or prompt prefixes.

## Strict external interface contracts

When model output will be passed directly to a documented API request, module
interface, or function call, the interface contract must be visible to the
model. A Python signature, OpenAPI operation, JSON Schema, protobuf definition,
or authoritative docstring is not automatically available to an ordinary
model request.

Use the slots as one integration contract:

| Slot | Integration responsibility |
|---|---|
| `input` | Request-specific values and source facts. |
| `info` | The authoritative API/schema documentation, signature, docstring, field semantics, and declared constraints. |
| `instruct` | How to transform the input, what callable or operation is being targeted, and how to handle missing information. |
| `output` | The exact machine-consumable type and nested shape expected by the downstream interface. |

For every downstream-consumed output field, describe its meaning and declare
its type, requiredness, and any applicable enum, format, range, nullability, or
cross-field constraint. Reusing these authoritative interface facts is boundary
and output control, not business-logic intrusion. Business decisions that are
not part of the interface contract still belong in the owning application
policy, and the host should run deterministic validation before the real call.

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
        "Build one POST /tickets request body from the input facts.",
        "Follow the target operation contract exactly; do not add fields.",
    ])
    .output({
        "title": (
            str,
            "Non-empty ticket title accepted by POST /tickets.",
            "not_null",
        ),
        "priority": (
            Literal["low", "normal", "high"],
            "Required API enum: low, normal, or high.",
            True,
        ),
        "requester_id": (
            str,
            "Required requester identifier copied from the input.",
            "not_null",
        ),
    }, format="json")
    .async_start()
)
```

Setting a slot persistently:

```python
agent = (
    Agently.create_agent()
    .role("You are an Agently support assistant.", always=True)
    .info({"product": "Agently 4.x"}, always=True)
)
```

`always=True` keeps the slot at the agent level so it carries to every request the agent runs.

Setting a slot for one request:

```python
result = (
    agent
    .instruct(["Reply in fewer than 80 words.", "Never invent product names."])
    .input("How do I configure a model?")
    .output({"answer": (str, "answer", True)})
    .start()
)
```

`instruct(...)` here is per-request because `always=True` was not passed.

## Agent vs execution scope

| Scope | API |
|---|---|
| Agent definition (persists for every future execution) | `.define(...)`, `.role(..., always=True)`, `.info(..., always=True)`, `.set_agent_prompt(key, value)` |
| AgentExecution draft (one execution only) | `.input(...)`, `.output(...)`, `.set_execution_prompt(key, value)` |

The slot you set last wins for that scope, so you can override agent defaults in one execution without mutating the agent.

## YAML / JSON prompt files

Same slot model, written declaratively:

```yaml
# prompts/triage.yaml
$ensure_all_keys: true
.agent:
  system: You are a ticket triage assistant.
  info:
    severities: ["P0", "P1", "P2", "P3"]
.execution:
  instruct: Classify the ticket text.
  output:
    $format: json
    severity:
      $type: str
      $desc: One of P0/P1/P2/P3
      $ensure: true
    rationale:
      $type: str
      $desc: One-line reason
      $ensure: true
```

Loading:

```python
agent = Agently.create_agent().load_yaml_prompt("prompts/triage.yaml")

result = (
    agent
    .create_execution()
    .set_execution_prompt("input", "Login fails for all users in EU region.")
    .start()
)
```

`load_json_prompt(...)` is the same API for JSON. Both accept either a path or a raw string body. Pick one config file per prompt or stack multiple prompts with `prompt_key_path="demo.output_control"` to select inside a multi-prompt file.

Prompt config uses `.execution` for one execution. Turn/request-scoped prompt
config aliases are removed; update older prompt files to `.execution`.

`$ensure_all_keys: true` at the top makes all leaves required regardless of per-leaf `$ensure`. Use it when the entire schema must come back complete.

`$format` on the `output` block maps to the same output format setting as
`.output(..., format=...)`. Supported values are `auto`, `json`,
`flat_markdown`, `hybrid`, `xml_field`, and `yaml_literal`. You can also use
`.format`, `$output_format`, or `.output_format` when a config file needs a more
explicit key.

## Round-tripping

You can convert a Python-built prompt back to YAML/JSON for review or storage:

```python
execution = agent.role("You are an Agently agent.", always=True).input("Say hello.").output({
    "reply": (str, "reply", True),
})
print(execution.get_yaml_prompt())
print(execution.get_json_prompt())
print(execution.get_prompt_text())  # rendered execution draft for pre-dispatch audit
```

This round-trip reviews the authored execution draft and its mappings. It is
not final-prompt evidence when runtime extensions can inject later.

## Placeholders

Inside any prompt slot, `{name}` references another slot by key, and `${name}` is replaced by `mappings={"name": "value"}` at load time. Common patterns:

- `instruct: "Reply {input} politely."` — pulls the request `input` into the instruct text.
- `${ENV.OPENAI_API_KEY}` in *settings* (not prompts) is replaced by the env var; prompts use `${name}` with explicit mappings.
- `${INPUT.customer}`, `${INFO.policy}`, and `${INSTRUCT.step}` are render-time
  slot references. They become prompt section pointers such as
  `[INPUT > customer]` instead of copying slot values into another slot. Slot
  names are case-insensitive; docs use uppercase. The path after the slot name
  is not validated because it is only a model-facing reference label.
- `${OUTPUT}` is an alias for `[OUTPUT REQUIREMENT]`.

To trigger placeholder substitution while loading, pass `mappings=...` explicitly:

```python
agent.load_yaml_prompt(yaml_text, mappings={"product_name": "Agently"})
```

## Where each layer's prompt comes from

When a request runs, Agently composes the model prompt from:

1. Agent-level slots (set with `always=True` or `set_agent_prompt`)
2. Request-level slots (set without `always=True`)
3. Slots populated by framework extensions or application code (Session injects chat history; retrieval code usually puts snippets into per-request `info(...)`)

Use `execution.get_prompt_text()` after one-run chaining, for example
`execution = agent.input(...).output(...)`, to inspect the rendered execution
draft before dispatch. It does not prove what late runtime injections add.
When the third layer can change the prompt, inspect the observed final
ModelRequest `prompt_text` after injection in a bounded test and redact secrets
before retaining it. `agent.get_prompt_text()` only inspects prompt data kept
on the Agent itself, such as slots set with `always=True`.

## See also

- [Schema as Prompt](schema-as-prompt.md) — leaf authoring, `$ensure`
- [Output Control](output-control.md) — what happens after parsing
- [Project Framework](../start/project-framework.md) — file layout for managing many prompts
