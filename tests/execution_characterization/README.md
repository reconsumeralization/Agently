# Execution characterization

These tests recover observable behavior from dev commit
`17f8ab170cfbc36427c75b0906a508cafcceff18` and compare the current implementation
with that frozen baseline plus individually recorded, approved changes.
This baseline was recovered **after** the initial refactor, not captured before it.

The transport replies are explicitly **synthetic**. They characterize framework
behavior, not model reasoning, factual accuracy, Prompt quality, real provider
streaming, latency, or release readiness. No model credentials are inherited by
the subprocesses. The requester is replaced; execution, parsing, validation,
Action dispatch, TriggerFlow, RecordStore, and file readback are real.

## Run

Normal CI needs neither the old Git object nor a network connection:

```sh
python -m pytest -q tests/test_agent_execution_characterization.py
```

For a fresh old/current comparison, export the fixed commit into an empty
temporary directory. No branch or worktree is needed. Substitute the actual
temporary path printed by `mktemp`; do not reuse a working checkout as the export
target. Run both versions with the same Python interpreter and dependencies.

```sh
mktemp -d /tmp/agently-characterization-baseline.XXXXXX
git archive 17f8ab170cfbc36427c75b0906a508cafcceff18 | tar -x -C /absolute/empty/temp-directory
python tests/execution_characterization/compare.py \
  --baseline-root /absolute/empty/temp-directory \
  --output /absolute/new/private-evidence-directory
```

The comparison command checks the old runtime's files against immutable Git
blob ids, runs every baseline case twice, verifies stability and the frozen
observations, and runs the same probe against the candidate. Each case uses a
fresh subprocess, verifies the imported package location, and has a 45-second
safety timeout. Additional dispatches or unused scripted replies fail the probe.
Full raw drafts/rendered Prompts are preserved with the private comparison report.

## Coverage

| Cases | Observable boundary |
|---|---|
| direct_text, direct_json, concurrent_readers | Parsed/full/text readers, captured readers and shared once-only production; ordered structured stream and completion provenance |
| mutation_and_fresh, fluent_isolation | Started-draft errors, explicit fresh factory, Agent defaults/history and per-request isolation; sync and async entrypoints |
| provider_failure, validation_repair, validation_exhaustion | Exception type/message, cached failure, final validator subjects and bounded request repair |
| review_warn, review_block, artifact_review, artifact_escape, default_review_artifact | Soft/blocking review, trusted artifact before review, exact UTF-8 file bytes/digests, containment and complete artifact input to default review |
| plan_ready, plan_clarification, plan_budget, plan_validation | Readiness/clarification/final-plan request topology, accepted exchange handoff, model budget and final-only validator |
| long_content, long_content_rejected | Section plan, predecessor continuity, ordered host assembly, final-only validation, no production replay after rejection |
| action_once | Real test-owned file Action executes once; blocking review and repeated readers do not repeat its effect |
| resume_flat, resume_taskboard | Seeded terminal state saved and loaded through real RecordStore; business/full views, original review context and zero model dispatch |
| goal_direct | Explicit direct strategy precedence and goal/success-criteria Prompt projection |
| ensure_short, ensure_continuation, ensure_stale, ensure_task_conflict | One-request fast path, lossless continuation, stale digest rejection and pre-dispatch task conflict |
| flat_task, taskboard_task | Actual bounded long-task planning/production/verification, context handoffs, terminal envelope and produced file bytes |

The two resume probes deliberately seed completed state; they do not claim to
exercise incomplete-task recovery. Rework, interrupt/pause/save/load, arbitrary
plugin behavior, multi-card concurrency, every output format, and real model
semantics need their own contract tests. Existing focused/full suites remain
required; this suite is not exhaustive coverage of every runtime path.

## Noise normalization and exact comparison

- Relocate only the explicitly created test workspace.
- Bind host-issued Execution, Workspace, exchange, Action-call, context-package,
  context-binding and nested request/run ids to stable labels. Preserve equality
  and reference relationships, including inside rendered Prompts and mapping
  keys. Do not pattern-rewrite arbitrary business UUIDs, references or digests.
- Normalize only `age_seconds`/`monotonic_time` inside nested
  `execution_meta.diagnostics.last_progress` and `stages.events`. Check finite,
  nonnegative values and monotonic event order. Other numeric values remain exact.
- Bind only the advisory `taskboard_card` verdict fingerprint in the observed
  TaskBoard acceptance-index input: it hashes full card metadata containing
  fresh runtime ids. Preserve its SHA-256 shape, identity relationships, all
  acceptance/cache facts and raw evidence. This suite does not validate that
  fingerprint algorithm; TaskBoard's own tests own it. **File/content digests
  are never normalized.**
- Keep complete ordered stream path/event/delta/completion projections. Full
  raw event metadata is not this snapshot's consumer contract; final results,
  policy payloads, provider-bound drafts and rendered Prompts are separately captured.

`fixtures/baseline.json` contains observed old behavior and its provenance.
`fixtures/approved_deltas.json` contains exact path/value changes with reasons,
not path wildcards. Long rendered-Prompt replacements are pinned by whole-text
SHA-256 to avoid duplicating them in the delta ledger. The old full text remains
in the baseline; paired raw evidence contains both full texts. Boolean and
numeric types, missing keys and list order are compared strictly.

The approved groups are Pattern-to-Execution migration, Prompt-owned goal
projections, and restored final-business review inputs. They do not authorize
unrelated output, request-count, instruction, schema or side-effect changes.
Comparator negative controls intentionally change those facts and must fail.

Do not refresh the baseline from current output to silence a failure. The
recording tools never accept deltas. `--freeze-baseline` can create a *missing*
fixture only after the immutable old implementation agrees with its repeat;
it refuses to overwrite an existing fixture. A probe or normalization-helper
change requires another old/current run and explicit evidence/provenance
reconciliation; pytest checks both source hashes. An intentional
runtime change requires review of its exact delta, not blanket snapshot updates.
