# Agently Repository Agent Rules

## Companion Skills Sync

- `../Agently-Skills` is the official companion repository for Agently coding-agent guidance.
- When modifying public APIs, recommended usage, deprecation policy, runtime behavior, examples, or user-facing docs, scan `../Agently-Skills` before considering the task complete.
- If `../Agently-Skills` is unavailable, report that companion sync was not completed.
- Do not add or strengthen a deprecation while leaving Agently-Skills examples to recommend that API as the default path.
- TriggerFlow changes must check Skills examples, references, route fixtures, native usage validation, and README guidance.
- Action Runtime, ModelResponse, Session, FastAPIHelper, settings, prompt config, tools, MCP, knowledge-base, and DevTools integration changes also require companion impact review.

## TriggerFlow Guidance

- Prefer async-first TriggerFlow examples for services, streaming paths, and long-running workers.
- Keep runtime stream behavior visible and testable.
- Treat `.end()` and result polling as compatibility surfaces once execution lifecycle close APIs are available.
- Prefer execution-local state over shared flow data for concurrent workflows.

## Editing Discipline

- Do not overwrite unrelated dirty files.
- Keep public examples aligned with the currently documented compatibility line.
- Update specs before or alongside implementation when an API contract changes.
