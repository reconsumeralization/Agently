# Agent Auto-Orchestration Examples

These examples demonstrate the 4.1.3 Agent auto-orchestration execution facade
with real model calls, realistic mock business data, and simulated I/O delays.

**Model calls are real** — every stage uses the LLM for classification,
summarization, drafting, and quality review. Model decision points
(skill selection, urgency classification, opportunity prioritization) are
genuine model outputs, never mocked.

**Business data is mocked** — commit logs, CRM tickets, product analytics,
and market signals are pre-seeded fake data representing what real systems
(GitLab, Salesforce, Amplitude, Crunchbase) would provide. Delays between
stages simulate real API fetch latency.

Run from the repository root:

```bash
python examples/agent_auto_orchestration/01_skills_dag_streaming.py
python examples/agent_auto_orchestration/02_actions_dag_streaming.py
python examples/agent_auto_orchestration/03_actions_skills_streaming.py
python examples/agent_auto_orchestration/04_education_lesson_plan_bilingual.py
python examples/agent_auto_orchestration/05_model_field_delta_streaming.py
```

Requires `DEEPSEEK_API_KEY` in the environment or a `.env` file.
Set `DYNAMIC_TASK_MODEL_PROVIDER=ollama` to use a local Ollama endpoint instead.

## Example summaries

### 01 — Release Notes Generator (Skills + DAG streaming)
A DevOps scenario: generate professional v2.5.0 release notes from a detailed
mock commit log (20 commits across features, fixes, docs, breaking changes,
security). A `release-notes-generator` skill defines five stages
(classify → summarize → validate → draft → compile), each backed by a
model-calling action. Simulated GitLab API fetch delay before classification.

**Mocked:** commit log, version metadata, team name.
**Real model:** classification, summarization, announcement drafting, QA.
**Key assertions:** `selected_route=skills`, four stage stream events,
feature/fix content present, announcement ready.

### 02 — Customer Support Triage (Actions + DAG streaming)
A customer support scenario: a P1-critical ticket from an enterprise customer
(Acme Corp, $120K/yr, 520 users, 1-hour SLA) where payment processing failed
after a deployment. The full CRM ticket context (customer profile, recent
changes, previous tickets, environment details) is mocked. Four DAG nodes
(classify → analyze → draft → review) with dependency edges, each making
model calls. Simulated CRM fetch and deployment log delays.

**Mocked:** ticket data, customer profile, environment details, change history.
**Real model:** urgency classification, root cause analysis, reply drafting,
enterprise QA review.
**Key assertions:** `selected_route=dynamic_task`, four task stream events,
valid urgency, draft present, quality approved.

### 03 — Market Research Brief (Actions + Skills streaming)
A product management scenario: research an "AI code review assistant" feature
for DevFlow ($14.2M ARR, 8,500 users). Detailed mock business context includes
internal survey data (n=1,200, pain point rankings), 6-week beta test results
(47% time reduction, NPS 72), and market signals (competitor funding, analyst
coverage). A `market-research-brief` skill defines four stages
(gather → analyze competitors → identify opportunities → compile), each
calling the model. Simulated Amplitude/G2/Crunchbase fetch delays.

**Mocked:** product analytics, survey results, beta metrics, market signals.
**Real model:** market landscape analysis, competitor profiling, opportunity
identification, executive summary generation.
**Key assertions:** `selected_route=skills`, four stage stream events,
competitor and opportunity content present, brief compiled.

### 04 — Bilingual Lesson Plan Generator (education business case)
An EdTech scenario where a single skill generates a bilingual Chinese/English
lesson package from a natural-language topic description. Each action stage
calls the model to produce structured content.

Demonstrates:
- real model calls inside async action stages (no mocking)
- multi-stage state passing via `${state.STAGE_ID}` templates
- `validate` stage gating downstream stages
- `emit` stage signalling package readiness
- streaming with `task_dag.tasks.*` and `skills.stages.*` events
- final deliverable checklist + AI-generated teacher summary
- identical skill handling Chinese and English task inputs

### 05 — Operator-visible Field Delta Streaming
A support operations scenario: a submitted Dynamic Task DAG contains several
`kind="model"` nodes. The CLI filters selected AgentExecution paths such as
`task_dag.tasks.prethink.fields.prethinking` and
`task_dag.tasks.reply.fields.reply`, then prints `item.delta` with
`print(delta, end="", flush=True)` so process notes and reply text appear while
the model is still generating those fields.

**Mocked:** ticket context, safe-action list.
**Real model:** prethinking, tool-call note, customer reply, quality reflection.
**Key assertions:** `selected_route=dynamic_task`, field delta events appear
for prethinking/tool-call-note/reply/reflection before task completion.
