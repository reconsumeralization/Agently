# Skills Executor Examples

These examples use `Agently.skills_executor` and the **standard `SKILL.md`**
model: a Skill is guidance plus optional execution metadata in frontmatter
(`execution: staged`, `allowed-tools`, `stages`, `max-steps`). There is no
`skill.yaml` and no custom stage schema. The default strategy is `single_shot`;
`staged` and `react` run on TriggerFlow and tool/action calls delegate through
Action / ActionRuntime. Side effects such as file writes, network calls, package
installation, and durable approvals remain owned by host code, Action,
ExecutionEnvironment, or TriggerFlow.

Self-authored example Skills live as real standard Skill directories under
`examples/skills_executor/skills/<skill-id>/SKILL.md`. Example scripts install
those directories; they do not construct inline `SKILL.md` strings or generate
root-level YAML manifests at runtime.

Configure the registry once with the public API:

```python
Agently.skills_executor.configure(registry_root=..., allowed_trust_levels=["local"])
```

| Example | Purpose |
|---|---|
| `01_basic_declarative_skills.py` | Model-free smoke test: install a standard `SKILL.md`, inspect the normalized contract, and resolve a deterministic `required` plan (registry + planner mechanics, no model call). |
| `02_deepseek_external_skill_cards.py` | Declare real external `SKILL.md` sources (Anthropic + Agently-Skills) through `agent.use_skills(...)`; the planner lazily discovers/materializes the selected remote Skill and DeepSeek applies its guidance. |
| `03_stock_research_business_minimal.py` | Minimal business sample: the **host** fetches live quotes (Stooq), then remote OctagonAI + Anthropic Skills provide stock-research and artifact guidance for a structured brief. |
| `04_dynamic_todo_triggerflow_realcase.py` | Diagnostic realcase: expose `Agently-Skills` guidance to DeepSeek and check whether it can generate a runnable dynamic TriggerFlow Todo-DAG without prompt-level API hints. |
| `05_combo_skillpack_diagnostics.py` | Combo Skill Pack diagnostics across five realcase packs (education, stock, travel, research-to-briefing, webapp acceptance). |
| `06_executable_education_course_pack.py` | Remote GarethManning education Skills + Anthropic docx/pdf/pptx/xlsx Skills produce structured course content; the **host** writes real artifacts (libraries host-managed, skipped if missing). |
| `07_agently_skills_availability_check.py` | Developer pre-flight: install the local `../Agently-Skills` catalog and verify explicit-selection eligibility. |
| `08_architecture_diagram_skill.py` | A prompt-first architecture-diagram Skill renders a self-contained dark-themed HTML+SVG diagram; the host writes the file. |
| `09_runtime_planner_effort_strategy.py` | Real-model runtime planner demo: `effort="fast"` takes the low-cost `single_shot` path; `effort="normal"` runs the complete preflight → research → plan → execute → verify → reflect → finalize chain with model-pool stage routing. |
| `10_model_pool_key_pool_resolution.py` | Public model-pool/key-pool resolution probe: `model_key` routes model and key settings through real request and Skills context calls. |

## The new-standard shape

```python
# Local authoring path: Skill = SKILL.md guidance plus optional standard frontmatter
Agently.skills_executor.configure(registry_root=reg_dir, allowed_trust_levels=["local"])
contract = Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)
skill_id = contract["skill_id"]          # slug of the frontmatter `name`

# 2. Default single_shot request; structured output via output
execution = await agent.async_run_skills_task(
    task,
    skills=[skill_id],
    mode="required",                      # default is "model_decision"
    output={"summary": (str, "..."), "items": ([str], "...")},
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
  and filter items where `type == "skills.model_stream"` and `is_completed`.
- To select quality/cost profiles, pass `effort="fast" | "normal" | "max"` to
  `async_run_skills_task(...)`. Advanced callers can override presets with
  `agent.set_settings("effort_presets", {...})`.

For remote/public Skills, the recommended business path is declaration, not
request-path preinstallation:

```python
agent.use_skills(
    [{"source": "anthropics/skills", "subpath": "skills/docx", "trust_level": "remote"}],
    mode="required",
)

execution = await agent.async_run_skills_task(
    "Draft a client-ready incident report as a docx package.",
    mode="required",
    effort="normal",
    output={...},
)
```

`install_skills_pack(...)` remains useful for prewarming/offline mirrors, CI
fixtures, and explicit local pool maintenance.

## Minimal business shape — host tool + prompt-only Skill

`03_stock_research_business_minimal.py` keeps the visible code close to how an
application developer would use the feature. The controlled side effect (fetching
quotes) runs in the **host**, before model analysis. Research guidance comes from
remote OctagonAI Skills; artifact guidance comes from Anthropic Skills:

```python
agent.use_skills([
    {"source": "OctagonAI/skills", "subpath": "skills/market-analyst-master"},
    {"source": "OctagonAI/skills", "subpath": "skills/financial-analyst-master"},
    {"source": "OctagonAI/skills", "subpath": "skills/sec-analyst-master"},
    {"source": "OctagonAI/skills", "subpath": "skills/earnings-analyst-master"},
    {"source": "anthropics/skills", "subpath": "skills/docx"},
    {"source": "anthropics/skills", "subpath": "skills/xlsx"},
], mode="required")

market_data = fetch_equity_market_data("NVDA, AMD, AVGO")   # host tool, real I/O

execution = agent.run_skills_task(
    f"Analyze NVDA/AMD/AVGO. market_data: {market_data}",
    mode="required",
    effort="normal",
    output={...},
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

`02_deepseek_external_skill_cards.py` uses `agent.use_skills(...)` remote source
selectors. Selectors accept GitHub shorthand (`anthropics/skills`,
`AgentEra/Agently-Skills`) with `subpath`, or local checkouts from
`ANTHROPIC_SKILLS_REPO` / `AGENTLY_SKILLS_REPO`. The planner discovers and
installs only when the source is selected.

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
python examples/skills_executor/09_runtime_planner_effort_strategy.py
python examples/skills_executor/10_model_pool_key_pool_resolution.py
```

Skills test suite:

```bash
PYTHONPATH=. python -m pytest -q tests/test_skills_executor.py tests/test_skills_executor_real_anthropic_skills.py
```
