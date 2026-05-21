# Agent Auto-Orchestration Examples

These examples cover two complementary layers of the 4.1.3 Agent execution facade:

- **Examples 01–03** are local process-stream smoke cases. They verify route selection,
  TriggerFlow/Dynamic Task stream bridging, and response-style consumption through
  `agent.create_execution()` without business-domain content.

- **Examples 04–05** are business-scenario cases. They demonstrate real-world usage
  patterns on top of the same execution facade.

Run from the repository root:

```bash
python examples/agent_auto_orchestration/01_skills_dag_streaming.py
python examples/agent_auto_orchestration/02_actions_dag_streaming.py
python examples/agent_auto_orchestration/03_actions_skills_streaming.py
python examples/agent_auto_orchestration/04_education_lesson_plan_bilingual.py
python examples/agent_auto_orchestration/05_agently_skills_availability_check.py
```

## Example summaries

### 01 — Skills + DAG streaming (smoke case)
Installs a minimal local skill and validates that Skills Executor stages compile
through Dynamic Task / Task DAG and surface process stream checkpoints through
`agent.create_execution()`.

### 02 — Actions + DAG streaming (smoke case)
Submits an explicit task graph with Action-backed nodes and validates that DAG
compilation, action execution, and stream bridging work end-to-end.

### 03 — Actions + Skills streaming (smoke case)
Verifies that a Skill stage calling an Action surfaces both stage-level and
action-level progress through the Agent stream.

### 04 — Bilingual lesson plan generator (education business case)
An EdTech scenario where a single skill generates a bilingual Chinese/English
lesson package from a natural-language topic description. **Requires a real
model API key** — each action stage calls the model to produce structured content.

Demonstrates:
- real model calls inside async action stages (no mocking)
- natural-language progress output during each stage
- multi-stage state passing via `${state.STAGE_ID}` templates in action inputs
- `validate` stage gating downstream stages on required state keys
- `emit` stage signalling package readiness into the runtime stream
- streaming consumption with `task_dag.tasks.*` real-time events and
  `skills.stages.*` post-execution confirmations
- final deliverable checklist + AI-generated teacher summary printed at completion
- identical skill handling Chinese and English task inputs without change

### 05 — Agently-Skills pack availability check (developer pre-flight)
Installs the active skills from a local `Agently-Skills` clone, lists each skill's
purpose and activation hints, and runs a deterministic plan-resolution check for
every skill without a model API call.

Demonstrates:
- `install_skills_pack()` for bulk installation from a local repository
- `list_skills()` and `inspect_skills()` for registry inspection
- `resolve_skills_plan(..., planner_mode="deterministic")` as a lightweight
  "does this skill pass eligibility filters?" pre-flight gate
- reading activation hints to understand how tasks route to skills

Requires a local clone of `AgentEra/Agently-Skills` at `../Agently-Skills`
relative to the Agently repository root.

---

Recommended model-owned auto-planning examples still live under
`examples/dynamic_task/` and use DeepSeek or local Ollama.
