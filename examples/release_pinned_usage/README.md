# Release-Pinned Developer Usage Examples

This directory contains developer usage examples selected as release gates.
They protect recommended public usage shapes across releases.

Policy:

- Selected scripts are listed in `pinned_usage_manifest.json`.
- A selected script must not be edited, replaced, or removed without explicit
  maintainer confirmation for that release.
- If a selected script fails because the recommended usage shape changed, stop
  the release check and ask whether the release should accept that usage update.
- Release example checks run with an explicit all-allowed test capability policy
  when a script may exercise Skills, Actions, TaskWorkspace, network, Python, shell,
  HTTP, browse, search, or MCP capability loading. This test posture is separate
  from Agently's default fail-closed runtime permission posture.
- Additive scripts may be proposed for new release claims, but selection should
  be recorded in the manifest before the script becomes a release gate.

The current pinned set covers AgentExecution result readers and fluent
`input().info().instruct().output()` identity, AgentExecution stream/key reader
facades, SkillLibrary-backed installation followed by AgentExecution
exact-revision binding, and the human-readable simple/detail debug console
profiles while EventCenter keeps the complete event stream. It also locks
`input().info().use_action()` to one execution and verifies that a terminal
Action-or-Response round becomes the existing AgentExecution result without a
redundant third model request. The 4.1.4.8 additions pin accepted retry streams,
and per-execution Skill scope. The withdrawn development-only script-candidate
projection is not a release gate. Script resources and explicit execution
authorization remain covered by `tests/test_skills_compatibility_facade.py` and
`examples/skills_executor/09_skill_script_exec.py`. This set
does not preserve the removed SkillsExecutor planning or prompt-injection
engine. Model-owned business behavior is checked by the model-backed examples
named in the manifest, including local Ollama/Qwen coverage for AgentExecution
delivery/review and the `plan` / `long_content` execution plugins.

## 4.1.4.8 Coverage

`pinned_usage_manifest.json` contains the authoritative
`development_line_4_1_4_8_coverage` mapping. The mapping is based on
`v4.1.4.7..dev` and links each runtime/public-use work batch to at least one
runnable example. Documentation-only prompt guidance and typing-only metadata
refinements remain covered by bilingual docs and static typing gates; they do
not claim a separate runtime behavior.
