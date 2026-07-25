---
title: Agently 4.1.4.4 Release Notes
description: Stronger Pydantic output constraints, recovery-aware TriggerFlow snapshots, and online-model-first release validation.
keywords: Agently, 4.1.4.4, Pydantic, structured output, TriggerFlow, snapshot, RecordStore
---

# Agently 4.1.4.4 Release Notes

Agently 4.1.4.4 strengthens two existing owner boundaries: `ModelRequest`
remains responsible for structured-output prompting and validation, while
TriggerFlow and its configured persistence provider remain responsible for
recoverable execution snapshots. No new facade or parallel runtime was added.

## Structured output constraints and correction

Pydantic v2 field constraints are now projected into supported structured-output
prompt formats, including requiredness, nullability, aliases, enum/literal
values, string and collection lengths, numeric ranges, patterns, and formats.
The original model remains the final acceptance authority.

```python
from pydantic import BaseModel, Field


class Ticket(BaseModel):
    title: str = Field(min_length=3, max_length=80)
    priority: int = Field(ge=1, le=3)
    labels: list[str] = Field(min_length=1, max_length=5)


ticket = (
    agent
    .input("Turn this incident report into a triage ticket.")
    .output(Ticket, format="json")
    .get_result()
    .get_data_object()
)
```

If parsed data fails Pydantic validation, Agently sends bounded field-level
correction feedback into the existing retry path. An invalid dictionary is not
returned as successful business data, and an accepted retry remains reusable
through object, data, and text result readers.

## Recovery-aware TriggerFlow snapshots

Issue [#331](https://github.com/AgentEra/Agently/issues/331) showed that repeated
save/load cycles could retain large completed signal values in every recovery
snapshot. 4.1.4.4 adds an opt-in projection policy for eligible terminal values:

```python
execution.set_snapshot_projection_policy(
    terminal_value_mode="digest",
    min_value_bytes=4096,
)
```

Pending interrupts and state required for recovery remain complete. Projected
terminal values retain a canonical SHA-256 digest and encoded size so duplicate
or conflicting resume requests can still be checked. Schema-v2 snapshots can
load prior schema-v1 full-value snapshots.

The built-in local `RecordStore` now keeps the latest three execution snapshot
versions per `run_id` by default:

```python
record_store = RecordStore(
    "./recovery",
    snapshot_retention={"keep_last": 5},
)

execution.set_snapshot_retention_policy(keep_last=2)
report = await execution.async_prune_recovery_snapshots(keep_last=1)
```

Use `{"keep_last": None}` to disable automatic pruning. Projection is owned by
TriggerFlow because it understands recovery semantics; physical retention is
owned by the persistence provider. Generic `put_checkpoint(...)` writes are not
automatically pruned.

## Release validation policy

Default `pytest` no longer depends on a locally running Ollama service or a
pre-pulled Ollama model. Deterministic OpenAI-compatible protocol tests remain in
the normal suite. Real-model release evidence is run separately against an
explicitly configured online model so the provider, model, request count, and
observed result can be recorded honestly.

## Core changes and upgrade impact

| Area | What changed | Recommended usage | Compatibility and risk | Evidence |
|---|---|---|---|---|
| Structured output | Supported Pydantic field constraints enter prompts and validation failures enter bounded retries. | Keep the `BaseModel` class as the `.output(...)` contract. | Additive correction of previously under-specified prompts; custom validators remain host-side only. | Prompt-generator, validation, result-reuse, and typing tests. |
| Snapshot projection | TriggerFlow can digest eligible terminal interrupt values and completed resume metadata. | Opt in for executions whose completed values dominate snapshot size. | Full values remain the default; pending recovery data is never projected. | Issue #331 A/B run: 1,307,086 B default versus 106,782 B digest projection, a 91.83% reduction. |
| Snapshot retention | The built-in local provider keeps the latest three versions by default. | Configure provider defaults or an execution-level override; use explicit prune for maintenance. | Intentional local-provider default change. `keep_last=None` preserves all versions. | Retention, override, save/load, registry, and provider-port tests. |
| Model validation | Local Ollama calls are removed from default `pytest`. | Use explicit bounded online-model experiments for release evidence. | Test-policy change only; Ollama remains usable as a configured OpenAI-compatible endpoint. | Deterministic mock coverage plus the release evidence record. |
| Deferred | Whole-snapshot byte ceilings, artifact-reference offloading, and distributed-provider retention implementations are not included. | Keep large business artifacts outside recovery snapshots and implement retention at each persistence provider boundary. | No claim of a universal snapshot size limit. | Explicit limitation from the #331 experiment and owner-boundary review. |
| Deferred | Concrete gVisor and Seatbelt providers tracked by #324 remain contributor-owned. | Use the released provider-neutral `ExecutionResource` seam or an explicitly authorized provider. | Not a 4.1.4.4 core-runtime blocker; no unreviewed sandbox implementation is bundled. | Issue #324 and PR status review. |

## Validation

Observed release-candidate validation:

- source Pyright over `agently/`, `tests/`, and `examples/`: 0 errors;
- clean-worktree default suite: 2,438 passed and 27 skipped; 25 skips are
  maintainer-local spec-runner checks and all 25 passed separately with the
  nested spec repository mounted, while the remaining two require an optional
  Anthropic Skills checkout;
- all three release-pinned deterministic usage scripts passed;
- the TriggerFlow durable-recovery example preserved load, latest-N retention,
  explicit prune, idempotent resume, and durable-event effects;
- wheel and source distribution built successfully; a fresh Python 3.10
  environment installed the wheel, found `py.typed`, exercised the structured
  missing-dependency error, and passed an installed-package Pyright smoke;
- one bounded DeepSeek `deepseek-v4-flash` request returned the declared
  Pydantic model in one request, preserved priority and labels, and satisfied
  all length/count/range constraints. Deterministic tests remain the evidence
  for invalid-first-attempt correction and retry reuse.

## Compatibility

- Package version: `4.1.4.4`.
- Release manifest: `compatibility/releases/4.1.4.4.json`.
- Python: `>=3.10`.
- Recommended DevTools version remains `agently-devtools >=0.1.10,<0.2.0`.
- Skills authoring protocol remains `agently-skills.authoring.v2`.
