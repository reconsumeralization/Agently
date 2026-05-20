# Skills Executor Examples

These examples cover both low-level executor mechanics and model-facing
SkillCard disclosure.

| Example | Purpose |
|---|---|
| `basic_declarative_skill.py` | Install a local declarative skill, resolve it in required mode, and execute an action stage. |
| `deepseek_external_skill_cards.py` | Install real external SKILL.md packages from `../Agently-Skills` and `anthropics/skills`, then verify DeepSeek sees and applies the selected SkillCard plus bounded primary guidance in `model_decision` mode. |
| `realcase_dynamic_todo_triggerflow.py` | Diagnostic realcase: expose `Agently-Skills` guidance to DeepSeek and check whether it can generate a runnable dynamic TriggerFlow Todo-DAG executor without prompt-level API hints or repair rounds. |
| `combo_skillpack_diagnostics.py` | Combo Skill Pack diagnostics for realcase orchestration: education course pack, stock research pack, travel planning pack, research-to-briefing pack, and webapp acceptance pack. |

`deepseek_external_skill_cards.py` requires `DEEPSEEK_API_KEY` in the shell or a
`.env` file. It clones `https://github.com/anthropics/skills.git` into
`.example_runtime/skills_executor/anthropic-skills` unless
`ANTHROPIC_SKILLS_REPO` points to an existing checkout. It uses
`../Agently-Skills` unless `AGENTLY_SKILLS_REPO` is set.

`realcase_dynamic_todo_triggerflow.py` also requires `DEEPSEEK_API_KEY` and
`../Agently-Skills` or `AGENTLY_SKILLS_REPO`. It is intentionally diagnostic:
the prompt does not spell out TriggerFlow API details. The host script evaluates
whether the model-generated module used real Agently APIs and whether it ran.

`combo_skillpack_diagnostics.py` requires `DEEPSEEK_API_KEY`. It uses local
checkouts when present and skips missing public Skill Pack sources instead of
mocking them. Pass `--fetch-missing` to clone the public repos into
`.example_runtime/skills_executor/combo_skillpacks/external` before running.
Useful source overrides:

- `TRAVEL_PLANNER_SKILL_REPO`
- `EDUCATION_AGENT_SKILLS_REPO`
- `OCTAGON_SKILLS_REPO`
- `CLAUDE_TRADING_SKILLS_REPO`
- `ANTHROPIC_SKILLS_REPO`

The combo diagnostic checks model-owned skill selection, stage switching,
intermediate artifacts, approval boundaries, fallbacks, external API boundaries,
and expected output coverage. It does not execute SaaS writes or generate fake
artifact files; its report is written to
`.example_runtime/skills_executor/combo_skillpacks/combo_skillpack_diagnostics.json`.

The same five combo cases are registered as benchmark tests in
`tests/test_skills_executor_combo_benchmarks.py`.

Fast source/install benchmark, no model call:

```bash
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_combo_benchmarks.py
```

Full DeepSeek benchmark, all five cases:

```bash
AGENTLY_RUN_SKILLS_BENCHMARKS=1 \
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_combo_benchmarks.py -m skills_benchmark
```
