# Skills Executor Examples

These examples are ordered from executor mechanics to real business benchmarks,
similar to `examples/dynamic_task`.

Use `Agently.skills_executor`. Skills Executor is still unreleased, so the
feature branch does not retain a separate `Agently.skills` compatibility alias.

| Example | Purpose |
|---|---|
| `01_basic_declarative_skills.py` | Low-level smoke test: install one local declarative Skill and execute an Action stage through `agent.run_skills_task(...)`. |
| `02_deepseek_external_skill_cards.py` | Install real external `SKILL.md` packages and verify DeepSeek receives and applies selected SkillCards in `model_decision` mode. |
| `03_stock_research_business_minimal.py` | Minimal business-facing sample: install a local Skill Pack by pack name, attach it with `agent.use_skills_packs(...)`, and get a structured stock research brief. |
| `04_dynamic_todo_triggerflow_realcase.py` | Diagnostic realcase: expose `Agently-Skills` guidance to DeepSeek and check whether it can generate a runnable dynamic TriggerFlow Todo-DAG executor without prompt-level API hints or repair rounds. |
| `05_combo_skillpack_diagnostics.py` | Combo Skill Pack diagnostics for realcase orchestration: education course pack, stock research pack, travel planning pack, research-to-briefing pack, and webapp acceptance pack. |
| `06_executable_education_course_pack.py` | Real execution benchmark for the education course pack: plan with external Skill Packs, install missing local artifact-writer dependencies through a Skills Executor action stage, generate real docx/pdf/pptx/xlsx/json artifacts, and judge content semantically. |

## Recommended Reading Order

1. Run `01_basic_declarative_skills.py` if you want to confirm the executor,
   declarative stages, and Action logs work without a model call.
2. Run `03_stock_research_business_minimal.py` if you want to see the shortest
   developer-facing business API: install a pack, attach the pack, send the
   task, receive a structured result.
3. Run `02_deepseek_external_skill_cards.py` when you need to verify real
   external SkillCard disclosure with DeepSeek.
4. Run `04_dynamic_todo_triggerflow_realcase.py` when you want a diagnostic
   check for model-generated DAG/executor behavior. It prints pass/fail as data
   and exits 0 by default; pass `--strict-exit` when using it as a CI gate.
5. Run `05_combo_skillpack_diagnostics.py` and
   `06_executable_education_course_pack.py` as benchmark/acceptance examples.

## Minimal Business Shape

`03_stock_research_business_minimal.py` intentionally keeps the visible code
close to how an application developer would use the feature:

```python
Agently.skills_executor.install_skills_pack(
    local_pack_dir,
    name="equity-research-demo",
    trust_level="local",
    update=True,
)

agent = Agently.create_agent("stock-research-demo")
agent.register_action(
    name="fetch_equity_market_data",
    desc="Fetch current public quote data from a controlled market-data source.",
    kwargs={"task": (str, "Task text containing ticker symbols.")},
    func=fetch_equity_market_data,
)

market_data = agent.run_skills_task(
    "Fetch current market quote data for NVDA, AMD, and AVGO.",
    skills_packs=["equity-research-demo"],
    mode="required",
).output["fetch_current_market_data"]

result = (
    agent
    .use_skills_packs(["equity-research-demo"], mode="model_decision", scope="request")
    .input({"task": "...", "market_data": market_data})
    .instruct("Use the disclosed equity research skill if it fits.")
    .output({...})
    .start()
)
```

The current public quote facts are fetched through a controlled Skills Executor
Action stage before model analysis. The example uses Stooq's CSV quote endpoint,
so it does not require a financial API key, but the provider timestamps may be
delayed and are not exchange-direct realtime ticks. The model still owns the
comparison, synthesis, and final research wording. The output is explicitly
research-only and should not include buy/sell/hold/order instructions.
Network 503/504 and timeout errors are retried. If the quote source remains
unavailable, the Action degrades to the last successful local quote cache and
marks `market_data.data_status="degraded"`; if no cache exists, the affected
ticker is returned as unavailable instead of using fake sample data.

## Environment

Model examples load `.env` themselves. `DEEPSEEK_API_KEY` can be available in
the shell or in a `.env` file. `03_stock_research_business_minimal.py` also
supports the shared Dynamic Task example fallback:

```bash
DYNAMIC_TASK_MODEL_PROVIDER=ollama \
python examples/skills_executor/03_stock_research_business_minimal.py
```

`02_deepseek_external_skill_cards.py` clones
`https://github.com/anthropics/skills.git` into
`.example_runtime/skills_executor/anthropic-skills` unless
`ANTHROPIC_SKILLS_REPO` points to an existing checkout. It uses
`../Agently-Skills` unless `AGENTLY_SKILLS_REPO` is set.

`04_dynamic_todo_triggerflow_realcase.py` requires `DEEPSEEK_API_KEY` and
`../Agently-Skills` or `AGENTLY_SKILLS_REPO`. It is intentionally diagnostic:
the prompt does not spell out TriggerFlow API details. The host script evaluates
whether the model-generated module used real Agently APIs and whether it ran.

`05_combo_skillpack_diagnostics.py` requires `DEEPSEEK_API_KEY`. It uses local
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
intermediate artifacts, approval boundaries, fallbacks, external API
boundaries, and semantic output coverage. It exercises the public executor
path:

```python
execution = agent.run_skills_task(
    case.task,
    skills=candidate_skill_ids,
    mode="model_decision",
    scope="execution",
    semantic_outputs=case.expected_outputs,
    planner_mode="model",
    planner_max_revisions=2,
)
```

The executor asks the model to compose the multi-skill behavior loop, evaluates
and repairs the plan against the semantic deliverable contract, maps the
resulting stage graph to Dynamic Task DAG execution, then exposes the plan and
close snapshot in the report. The full benchmark also runs a second Agently
model-judge request with output control: per-rule evidence and concise reasons
are emitted before each boolean judgment, and the final `passes` field is last.
It does not execute SaaS writes or generate fake artifact files; its report is
written to
`.example_runtime/skills_executor/combo_skillpacks/combo_skillpack_diagnostics.json`.

`06_executable_education_course_pack.py` is the first execution-grade
benchmark. Before writing artifacts, it installs a local dependency-installer
Skill and runs it through `agent.run_skills_task(...)`. That Skill calls a
controlled `ensure_python_packages` Action, which installs missing writer
libraries such as `python-docx`, `openpyxl`, `python-pptx`, `reportlab`, and
`pypdf`. Dependency failure is not treated as a degraded success: the benchmark
fails closed unless the Skills Executor action log shows that every required
import is available. After dependency repair, the benchmark writes real files
and uses a second output-controlled Agently model judge for semantic content
validation. The script prints the dependency action status and import
availability alongside the final result, so dependency repair remains visible
in the example output. Missing public Skill repositories are fetched by
default; pass `--no-fetch-missing` to require existing local checkouts.

## Commands

```bash
python examples/skills_executor/01_basic_declarative_skills.py
python examples/skills_executor/03_stock_research_business_minimal.py
python examples/skills_executor/02_deepseek_external_skill_cards.py
python examples/skills_executor/04_dynamic_todo_triggerflow_realcase.py
python examples/skills_executor/05_combo_skillpack_diagnostics.py --fetch-missing
python examples/skills_executor/06_executable_education_course_pack.py
```

Fast source/install benchmark, no model call:

```bash
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_combo_benchmarks.py
```

Full DeepSeek benchmark, all five cases:

```bash
AGENTLY_RUN_SKILLS_BENCHMARKS=1 \
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_combo_benchmarks.py -m skills_benchmark
```

Real execution benchmark, education course package:

```bash
AGENTLY_RUN_SKILLS_REAL_EXECUTION=1 \
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_real_execution_benchmarks.py -m skills_real_execution
```
