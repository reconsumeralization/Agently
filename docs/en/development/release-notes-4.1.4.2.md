---
title: Agently 4.1.4.2 Development Notes
description: Development-line notes for the breaking Workspace storage and lifecycle simplification.
keywords: Agently, 4.1.4.2, Workspace, AgentTask, TriggerFlow, storage, retention
---

# Agently 4.1.4.2 Development Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.4.2.md)

Agently 4.1.4.2 is the current development target. It contains a breaking
Workspace redesign intended to keep ordinary runs close to zero persistent
overhead while preserving explicit recovery and durable-information use cases.

## Workspace Boundary

- `Workspace(root)` now exposes `root` itself as the ordinary file boundary.
- The default root is the entry script directory, with current working directory
  fallback.
- External files are readable and read-only by default. Use
  `mode="read_write"` or an approved file Action for mutation.
- `.agently` is the reserved private area. Its files, database, records,
  vectors, recovery, memory, and Skills state are created independently and
  only when used.
- New products that cannot be written externally use
  `.agently/files/<execution-id>/...`. There is no public `files_root`, generated
  Workspace guide, or framework-owned artifact-directory taxonomy.

## Terminal Storage

AgentExecution and AgentTask keep the full live process in memory and
observation output rather than duplicating it into Workspace. Terminal cleanup
keeps only selected fallback products whose trusted refs pass physical
readback; drafts, intermediate files, unselected products, and invalid refs are
removed. Ordinary external files are never cleanup targets.

AgentTask restart state is opt-in through:

```python
agent.create_task(
    goal="Prepare the report.",
    options={"agent_task": {"workspace_recovery": True}},
)
```

## TriggerFlow Durability

A TriggerFlow Workspace binding may provide direct file or record access, but
it no longer becomes a RuntimeEvent store automatically. Save, pause, and load
may activate Workspace snapshot recovery when required. Durable RuntimeEvent
replay or Workspace-backed audit must bind `runtime_event_store` explicitly.
Ordinary audit remains the responsibility of logs, EventCenter sinks, or
DevTools.

Large TriggerFlow close results remain available to the in-process caller but
are omitted from the terminal RuntimeEvent projection once they exceed the
bounded inline limit. A large result with selected file products projects only
their compact refs. TriggerFlow does not create a Workspace record solely to
carry that event result.

## State Assignment

`StateData.set(...)` and item assignment now replace the existing target value,
including lists, mappings, sets, and empty collections. Recursive composition
remains explicit through `StateData.update(...)`; list accumulation remains
explicit through `append(...)` / `extend(...)`. TriggerFlow `set_state(...)`,
`async_set_state(...)`, and flow-data setters therefore record the exact new
value instead of retaining stale collection members. This prevents cleared
queues, restored snapshots, and TaskBoard progress mappings from silently
growing across ticks.

## AgentTask Lifecycle And Terminal Repair

AgentTask now runs one versioned TriggerFlow lifecycle graph with visible
context, plan, work, materialization, evidence, verification, and transition
nodes. Stage events carry only host-issued frame/plan/work/evidence ids and a
monotonic state version; stale and cross-task signals fail closed. TaskBoard is
the nested work producer inside the work node and returns to the same terminal
transition as Flat.

One semantic terminal verifier now owns both `criterion_checks` and
`material_claim_checks` for the current terminal-carrier inventory. The old
claim-inventory, source-selection, per-claim judgment, and empty-inventory
review request chain is removed. The host assigns each exact current text span a
request-local `claim_key`; the model returns that key plus offered evidence
reference ids, and host code reconstructs carrier ids, quotes, paths, and
content versions from the immutable offered map.

Required capability evidence is evaluated before the semantic verifier. A
missing authored `action_succeeded` requirement therefore schedules its
Action-shaped repair without spending a verifier request, while an unavailable
required Action fails closed.

Material-claim repair for a trusted file-backed carrier uses the same
host-owned control path in Flat and TaskBoard. A dedicated structured
ModelRequest returns exactly one `old_string`/`new_string` replacement per
host-issued `claim_key`; the repair does not open a general
AgentExecution/ActionRuntime round. The host validates the authorized path,
current `content_version_id`, exact-match cardinality, and claim scope before
writing. Full-file writes, replace-all, stale versions, and unrelated edits
fail closed. Successful readback is promoted as a new content version.

TaskBoard now preserves the completed status of a sufficient control card when
`next_board_action=stop`; that field stops board progression and is not a card
failure signal. Material-claim patch control schemas require one operation for every
contracted `claim_key`, and TaskBoard applies the same immutable-version guard
as Flat. Host-generated Workspace patch/readback artifacts retain transport
role and are excluded from supporting their descendant carrier after copy or path changes.

Card execution status is also no longer vetoed by malformed model-authored
`evidence_use`. Canonical Action lifecycle facts own whether the business
operation succeeded; invalid bindings remain untrusted diagnostics, while the
single terminal verifier independently owns semantic acceptance. Finalizer
bindings may pin canonical evidence host-side, but they are not copied into the
terminal verifier as a second evidence-id selection domain.

Flat and TaskBoard also keep the artifact body, compact inline result, and
trusted refs as separate carriers. Explicit inline results never become file
bodies. A successful manifest-bound Action write/readback is promoted as the
trusted artifact even under an `inline_final` plan, cumulative trusted artifact
evidence remains available to later iterations, and terminal verification keeps
the same physical carrier/content version throughout repair. Terminal projection keeps
an explicit compact summary alongside the file refs instead of replacing it
with a pointer. Unknown or duplicate claim keys and unknown evidence ids fail
closed; model output cannot copy or redefine canonical carrier identities or
artifact quotes.

The same manifest path also has one write owner. Once a successful file Action
has written that path, AgentTask adopts the current physical Workspace readback;
it does not write a different `candidate_final_result` or `final_result` body
over the file during materialization. A later revision must be another explicit
file Action. If the Action reports success but the declared path cannot be read
back, delivery fails closed instead of falling back to the model-returned body.

When a required Workspace delivery path exists, only its current physical
readback is offered as the terminal Workspace carrier; intermediate working
files remain cold evidence. A malformed verifier response now receives one
merged structured repair contract covering every invalid response section plus
the current offered claim/evidence-key sets. The retry re-enters only
verification and transition, reuses the prepared final candidate, and therefore
does not repeat TaskBoard finalization or business work. The third occurrence of
the same stable protocol issue still fails closed. Resolved terminal-convergence
records are refreshed in the public diagnostics snapshot before a successful
result is emitted, so observers do not retain a stale active issue after
acceptance.

## Compatibility

This is a development-line breaking change. Removed interim Workspace layout
APIs are not retained as aliases. The released 4.1.4.1 compatibility manifest
and package version remain unchanged until release preparation starts;
`compatibility/in-development.json` owns the 4.1.4.2 target.

Acceptance experiments and full repository gates remain pending until the
feature branch is accepted.
