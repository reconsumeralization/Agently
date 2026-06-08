# Development

Read in this order:

1. [Coding Agents](coding-agents.md): using the Agently-Skills companion repo with Codex, Claude Code, Cursor, and similar tools.
2. [Skills Executor](skills-executor.md): framework-side runtime skill consumption through Agent APIs, plans, and Actions.
3. [Agently 4.1.3.6 Release Notes](release-notes-4.1.3.6.md): AgentExecution ownership, Result-first consumption, stream-end hardening, and bounded task-loop slice.
4. [Agently 4.1.3.5 Release Notes](release-notes-4.1.3.5.md): settings-owned output defaults, meaningful required values, AgentTurn prompt isolation, and `set_turn_prompt(...)`.
5. [Agently 4.1.3.4 Release Notes](release-notes-4.1.3.4.md): structured output parsing hardening, request retry, runtime capability policy, and AgentTaskLoop first public slice.
6. [Agently 4.1.3.3 Release Notes](release-notes-4.1.3.3.md): typed settings/options, model profiles, API key pool failover, runtime handler ownership, core package refactors, and image input.
7. [Agently 4.1.3.2 Release Notes](release-notes-4.1.3.2.md): bounded AgentExecution task steps, Workspace-backed step context, runtime stall control, and EventCenter RuntimeEvent delivery.
8. [Agently 4.1.3.1 Release Notes](release-notes-4.1.3.1.md): Workspace foundation, Recall skeleton, and explicit multi-turn task information management.
9. [Agently 4.1.3 Release Notes](release-notes-4.1.3.md): final 4.1.3 runtime goals, user-facing code shape, and business value.
10. [Release Workflows](release-workflows.md): current repository automation for docs, installers, and PyPI publishing.

DevTools belongs to [Observability](../observability/) because it consumes observation events. Action, MCP, and service APIs live in their own folders.
