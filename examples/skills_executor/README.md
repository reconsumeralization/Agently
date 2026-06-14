# Skills Executor Examples

These examples use the standard `SKILL.md` model: a Skill is guidance plus
indexed resources and metadata. There is no `skill.yaml` and no custom stage
schema. Direct `run_skills_task(...)` calls are compatibility facades over
Blocks lowering: `single_shot` lowers to a handler-backed `model_request` block,
while `runtime_chain`, `staged`, `react`, and custom labels lower to trusted
`flow_segment` blocks.

Older Skills examples that depended on pre-4.1.3.8 orchestration assumptions
were moved to:

```text
examples/archived/pre-4.1.3.8-skills-orchestration/skills_executor/
```

Those archived files are reference material only. They are not runnable
recommended examples for 4.1.3.8 and later and should not be force-fit to the
Blocks lifecycle.

## Current Examples

| Example | Purpose |
|---|---|
| `01_basic_declarative_skills.py` | Model-free smoke test: install a standard `SKILL.md`, inspect the normalized contract, and resolve a deterministic `required` plan. |
| `07_agently_skills_availability_check.py` | Developer pre-flight: install the local `../Agently-Skills` catalog and verify explicit-selection eligibility. |
| `08_architecture_diagram_skill.py` | Prompt-first architecture-diagram Skill rendered through the default `single_shot` Blocks compatibility route; the host writes the file. |
| `09_runtime_planner_effort_strategy.py` | Real-model compatibility-label demo: `effort="fast"` lowers to `model_request`; `effort="normal"` lowers to `flow_segment` and records Blocks close-snapshot evidence. |
| `10_model_pool_key_pool_resolution.py` | Public model-pool/key-pool resolution probe through real request and Skills context calls. |

## Current Shape

```python
Agently.skills_executor.configure(registry_root=reg_dir, allowed_trust_levels=["local"])
contract = Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)
skill_id = contract["skill_id"]

execution = await agent.async_run_skills_task(
    task,
    skills=[skill_id],
    mode="required",
    output={"summary": (str, "..."), "items": ([str], "...")},
    stream_handler=on_stream,
)

result = execution.output
```

- Pass `skills=[...]` or `skills_packs=[...]` to the direct
  `run_skills_task(...)` facade when the request must use selected Skills.
- Use `agent.use_skills(...).input(...).start()` for AgentExecution route
  candidate registration, or `agent.use_skills(..., always=True)` when you
  intentionally want session-level prompt/route registration.
- Host code, ActionRuntime, ExecutionResource, Workspace, or TriggerFlow own
  side effects such as file writes, network calls, package installation, and
  durable approvals.
- Inspect `execution.close_snapshot["blocks"]` when you need the compiled
  ExecutionPlan, ExecutionBlockGraph, ResultAdapter output, or EvidenceEnvelope.

For remote/public Skills on the direct facade, pass the source selector on the
request:

```python
execution = await agent.async_run_skills_task(
    "Draft a client-ready incident report as a docx package.",
    skills=[{"source": "anthropics/skills", "subpath": "skills/docx", "trust_level": "remote"}],
    mode="required",
    effort="normal",
    output={...},
)
```

`install_skills_pack(...)` remains useful for prewarming, offline mirrors, CI
fixtures, and explicit local pool maintenance.

## Commands

```bash
python examples/skills_executor/01_basic_declarative_skills.py
python examples/skills_executor/07_agently_skills_availability_check.py
python examples/skills_executor/08_architecture_diagram_skill.py
python examples/skills_executor/09_runtime_planner_effort_strategy.py
python examples/skills_executor/10_model_pool_key_pool_resolution.py
```

Focused test suite:

```bash
PYTHONPATH=. python -m pytest -q tests/test_skills_executor.py
```
