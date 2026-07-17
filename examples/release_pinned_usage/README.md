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

The first pinned set for 4.1.4.1 covers AgentExecution result readers,
AgentExecution stream/key reader facades, and the legacy SkillsExecutor
compatibility facade. Model-owned business behavior is still checked by the
model-backed examples named in the manifest.
