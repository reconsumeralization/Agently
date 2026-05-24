# Skills Executor Examples

These examples use `Agently.skills_executor` and the **standard `SKILL.md`**
model: a Skill is guidance plus optional execution metadata in frontmatter
(`execution: staged`, `allowed-tools`, `stages`, `max-steps`). There is no
`skill.yaml` and no custom stage schema. The default strategy is `single_shot`;
`staged` and `react` run on TriggerFlow and tool/action calls delegate through
Action / ActionRuntime. Side effects such as file writes, network calls, package
installation, and durable approvals remain owned by host code, Action,
ExecutionEnvironment, or TriggerFlow.

Configure the registry once with the public API:

```python
Agently.skills_executor.configure(registry_root=..., allowed_trust_levels=["local"])
```

| Example | Purpose |
|---|---|
| `01_basic_declarative_skills.py` | Model-free smoke test: install a standard `SKILL.md`, inspect the normalized contract, and resolve a deterministic `required` plan (registry + planner mechanics, no model call). |
| `02_deepseek_external_skill_cards.py` | Install real external `SKILL.md` packages (Anthropic + Agently-Skills) and verify DeepSeek receives and applies selected SkillCards in `model_decision` mode. |
| `03_stock_research_business_minimal.py` | Minimal business sample: the **host** fetches live quotes (Stooq), then a prompt-only Skill Pack writes a structured research brief from that data. |
| `04_dynamic_todo_triggerflow_realcase.py` | Diagnostic realcase: expose `Agently-Skills` guidance to DeepSeek and check whether it can generate a runnable dynamic TriggerFlow Todo-DAG without prompt-level API hints. |
| `05_combo_skillpack_diagnostics.py` | Combo Skill Pack diagnostics across five realcase packs (education, stock, travel, research-to-briefing, webapp acceptance). |
| `06_executable_education_course_pack.py` | A prompt-only Course Pack Designer Skill produces structured course content; the **host** writes real .docx/.pdf/.pptx/.xlsx/.json artifacts (libraries host-managed, skipped if missing). |
| `07_agently_skills_availability_check.py` | Developer pre-flight: install the local `../Agently-Skills` catalog and verify explicit-selection eligibility. |
| `08_architecture_diagram_skill.py` | A prompt-first architecture-diagram Skill renders a self-contained dark-themed HTML+SVG diagram; the host writes the file. |
| `09_staged_effort_strategy.py` | Real-model staged execution demo: `execution: staged` plus `effort_presets` maps caller-facing effort to `single_shot` or TriggerFlow-backed staged execution. |

## The new-standard shape

```python
# 1. Skill = SKILL.md guidance plus optional standard frontmatter
Agently.skills_executor.configure(registry_root=reg_dir, allowed_trust_levels=["local"])
contract = Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)
skill_id = contract["skill_id"]          # slug of the frontmatter `name`

# 2. Default single_shot request; structured output via semantic_outputs
execution = await agent.async_run_skills_task(
    task,
    skills=[skill_id],
    mode="required",                      # default is "model_decision"
    semantic_outputs={"summary": (str, "..."), "items": ([str], "...")},
    stream_handler=on_stream,             # optional: field-level streaming
)

# 3. Host owns side effects; TriggerFlow/Action own orchestration/acting paths
result = execution.output
write_file(result["summary"])             # persistence is host code
```

- `mode` defaults to `"model_decision"` (the planner asks the model which Skills
  to use and in what order). Pass `mode="required"` to force-select known Skills.
- Reference Skills by the `skill_id` returned from `install_skills(...)` — it is
  the slug of the frontmatter `name` (e.g. `"Release Notes Generator"` →
  `"release-notes-generator"`).
- To stream field-level progress, use `async_run_skills_task(..., stream_handler=...)`
  and filter items where `type == "skills.model_stream"` and `is_complete`.
- To select quality/cost profiles, configure `agent.set_settings("effort_presets", {...})`
  and pass `effort="fast" | "normal" | ...` to `async_run_skills_task(...)`.

## Minimal business shape — host tool + prompt-only Skill

`03_stock_research_business_minimal.py` keeps the visible code close to how an
application developer would use the feature. The controlled side effect (fetching
quotes) runs in the **host**, before model analysis; the Skill never touches the
network:

```python
Agently.skills_executor.install_skills_pack(local_pack_dir, name="equity-research-demo", trust_level="local", update=True)

market_data = fetch_equity_market_data("NVDA, AMD, AVGO")   # host tool, real I/O

execution = agent.run_skills_task(
    f"Analyze NVDA/AMD/AVGO. market_data: {market_data}",
    skills_packs=["equity-research-demo"],
    mode="required",
    semantic_outputs={...},
)
result = execution.output
```

It uses Stooq's CSV quote endpoint (no API key needed); provider timestamps may
be delayed and are not exchange-direct realtime ticks. The output is explicitly
research-only and excludes buy/sell/hold/order instructions.

## Environment

Model examples load `.env` themselves. `DEEPSEEK_API_KEY` can be in the shell or
a `.env` file; set `DYNAMIC_TASK_MODEL_PROVIDER=ollama` to use a local Ollama
endpoint instead. `01_basic_declarative_skills.py` needs no model.

`02_deepseek_external_skill_cards.py` clones `https://github.com/anthropics/skills.git`
into `.example_runtime/skills_executor/anthropic-skills` unless
`ANTHROPIC_SKILLS_REPO` points to an existing checkout, and uses
`../Agently-Skills` unless `AGENTLY_SKILLS_REPO` is set.

`06_executable_education_course_pack.py` writes Office artifacts when the optional
libraries are installed (`pip install python-docx reportlab python-pptx openpyxl`);
each artifact is skipped gracefully if its library is missing. The example does
**not** install packages at runtime — dependencies are the host's responsibility.

## Commands

```bash
python examples/skills_executor/01_basic_declarative_skills.py   # no model needed
python examples/skills_executor/02_deepseek_external_skill_cards.py
python examples/skills_executor/03_stock_research_business_minimal.py
python examples/skills_executor/04_dynamic_todo_triggerflow_realcase.py
python examples/skills_executor/05_combo_skillpack_diagnostics.py --fetch-missing
python examples/skills_executor/06_executable_education_course_pack.py
python examples/skills_executor/07_agently_skills_availability_check.py
python examples/skills_executor/08_architecture_diagram_skill.py
python examples/skills_executor/09_staged_effort_strategy.py
```

Skills test suite:

```bash
PYTHONPATH=. python -m pytest -q tests/test_skills_executor.py tests/test_skills_executor_real_anthropic_skills.py
```
