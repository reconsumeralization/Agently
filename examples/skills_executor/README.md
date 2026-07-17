# Skill examples

A Skill is an immutable, revisioned context package: `SKILL.md` guidance plus
indexed resources. `SkillLibrary` owns installation and package truth;
`AgentExecution` owns task-scoped selection and binding; `TaskContext` and
`ContextReader` own progressive disclosure.

`Agently.skills_executor` remains a thin management/compatibility facade. It
can configure the library, install/list/inspect/read Skills, build compatibility
context packs, and expose the TaskDAG helper. It does not choose execution
routes, run Skill-local strategies, actionize scripts, or grant capabilities.

Recommended execution:

```python
execution = (
    agent
    .use_skills([skill_id], mode="required")
    .input(task)
    .output({"summary": (str, "...")})
)
result = await execution.async_get_data()
```

Released convenience calls remain adapters over the same path:

```python
compat = await agent.async_run_skills_task(
    task,
    skills=[skill_id],
    mode="required",
    output={"summary": (str, "...")},
)
result = compat.output
```

Current runnable examples:

```bash
python examples/skills_executor/01_basic_declarative_skills.py
python examples/skills_executor/07_agently_skills_availability_check.py
python examples/skills_executor/08_architecture_diagram_skill.py
python examples/skills_executor/10_model_pool_key_pool_resolution.py
```
