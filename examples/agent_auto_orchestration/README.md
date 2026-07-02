# Agent Auto-Orchestration Examples

These examples are the current 4.1.3.8 development-line examples for
AgentExecution, Dynamic Task DAG, ActionRuntime, and the Blocks-backed Skills
compatibility path.

Older Skills auto-orchestration examples from before the 4.1.3.8 Blocks
lifecycle refactor were moved to:

```text
examples/archived/pre-4.1.3.8-skills-orchestration/agent_auto_orchestration/
```

Those archived files are reference material only. They should not be treated as
runnable examples or recommended usage for 4.1.3.8 and later, and should not be
forced onto the new Blocks lifecycle.

## Current Commands

Run from the repository root. Model examples need `DEEPSEEK_API_KEY` in the
environment or `.env`; set `DYNAMIC_TASK_MODEL_PROVIDER=ollama` for local
Ollama where supported.

```bash
python examples/agent_auto_orchestration/02_actions_dag_streaming.py
python examples/agent_auto_orchestration/05_model_field_delta_streaming.py
python examples/agent_auto_orchestration/06_parallel_dag_field_streaming.py
python examples/agent_auto_orchestration/19_remote_skills_weather_event_ops.py
python examples/agent_auto_orchestration/20_agent_execution_lineage_workspace_loop.py
python examples/agent_auto_orchestration/21_agent_execution_github_issue_intake.py
python examples/agent_auto_orchestration/22_unified_agent_execution_result.py
python examples/agent_auto_orchestration/23_agent_execution_auto_dispatch.py
python examples/agent_auto_orchestration/24_independent_dynamic_task_dag.py
```

`_TEMPLATE_standard_skill_orchestration.py` remains as the compact reference for
the default `single_shot` Skills compatibility facade. Direct Skills examples
should pass selected Skills to `run_skills_task(..., skills=[...])`; use
`agent.use_skills(...).input(...).start()` when the goal is AgentExecution route
candidate registration.

## Current Examples

- **02 - Customer Support Triage.** Independent Dynamic Task DAG with local
  handlers, dependency edges, and real model calls over mocked CRM data.
- **05 - Operator-visible Field Delta Streaming.** Independent Dynamic Task DAG
  with `kind="model"` nodes and field-level runtime streaming.
- **06 - Parallel DAG Field Delta Streaming.** Independent multi-branch Dynamic
  Task DAG with concurrent workstreams and a fan-in executive brief.
- **19 - Remote Skills Weather Event Ops.** Current Blocks-backed remote Skills
  acceptance example. Weather facts come from a real MCP server through
  ActionRuntime; selected Skills execute through `effort="normal"` and expose
  Blocks close-snapshot evidence.
- **20 - AgentExecution Lineage Workspace Loop.** Two-step AgentExecution
  lineage and Workspace persistence example.
- **21 - GitHub Issue Intake.** AgentExecution plus restricted shell Action for
  real GitHub CLI issue intake.
- **22 - Unified AgentExecution Result.** Minimal quick prompt plus task-loop
  strategy consumed through the same result/stream/meta facade.
- **23 - AgentExecution Auto Dispatch.** Route-selection example proving
  default `model_request` and task-strategy `agent_task` dispatch.
- **24 - Independent Dynamic Task DAG.** Infrastructure smoke for direct
  `Agently.create_dynamic_task(...)` submitted-DAG execution.

Model calls are real. Business data is mocked unless the example explicitly
states that it uses a real external system such as MCP or GitHub CLI.
