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
python examples/agent_auto_orchestration/06_parallel_dag_field_streaming.py
python examples/agent_auto_orchestration/07_code_security_audit.py
python examples/agent_auto_orchestration/08_web_research_report.py
python examples/agent_auto_orchestration/09_model_stage_field_streaming.py
python examples/agent_auto_orchestration/10_browser_web_testing.py
python examples/agent_auto_orchestration/11_branch_code_review.py
python examples/agent_auto_orchestration/12_model_plan_incident_response.py
python examples/agent_auto_orchestration/13_validate_emit_compliance_check.py
python examples/agent_auto_orchestration/14_deep_research.py
python examples/agent_auto_orchestration/15_agentic_research.py
python examples/agent_auto_orchestration/16_report_evaluation.py
python examples/agent_auto_orchestration/17_self_reflective_research.py
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

### 06 — Parallel DAG Field Delta Streaming (multi-branch)
A launch readiness assessment with three independent workstreams (Security,
Performance, UX) running concurrently after a shared context-loading step.
Each workstream has two serial model nodes (analysis → sign-off). A final
executive brief node waits for all three sign-offs (fan-in), synthesising a
go/no-go verdict with top cross-cutting risks.

**Stream topology:**
```
context ──┬── security_analysis ── security_signoff ──┐
          ├── perf_analysis ─────── perf_signoff ─────┤
          └── ux_analysis ───────── ux_signoff ───────┘
                                                       │
                                      executive_brief ←┘
```
**Mocked:** launch context with service inventory, infra changes, stats, known risks.
**Real model:** 7 model nodes producing security/perf/UX findings and sign-offs,
executive verdict with risk summary.
**Key assertions:** field deltas stream from every parallel workstream plus
executive synthesis, all 4 tracked signoff/executive tasks complete, and the CLI
can filter branch-specific field paths before task completion.

### 07 — Code Security Audit (file ops + real code scanning)
A DevSecOps scenario: scan a mock microservice codebase (6 files: Python, JS,
TypeScript, Go, SQL, YAML config) for security issues. A `code-security-audit`
skill defines five action stages (scan → grep secrets → grep injection → CVE
lookup → compile report), each performing real file I/O and pattern matching.
The CVE lookup queries a real vulnerability database for known CVEs on detected
dependency versions.

**Real:** file system scanning, regex secret/injection detection, CVE lookup via
osv.dev API, model-generated remediation recommendations.
**Key assertions:** `selected_route=skills`, all five stages complete, findings
categorized by severity, actionable recommendations generated.

### 08 — Web Research & Report Generation (network + model + file output)
A market intelligence scenario: research "AI agent observability" by searching
the web, fetching relevant pages, and synthesising a structured report. A
`web-research-report` skill chains search → fetch → synthesise → write. The
synthesise stage calls the model to distil raw web content into an executive
brief. The final stage writes the report to `~/.agently_reports/`.

**Real:** DuckDuckGo HTML search, httpx page fetching, model synthesis, file write.
**Key assertions:** `selected_route=skills`, 4 stages complete, report saved
with model-generated content, web results structured into findings.

### 09 — Product Launch Press Kit (Skill model stage field streaming)
A product marketing scenario: generate a press kit (positioning brief + risk
register) for a fictional "DevFlow Code Review AI" product launch. A
`product-launch-press-kit` skill uses two native `kind: model` stages that
stream `positioning_text` and `risks_text` through
`skills.stages.<stage_id>.fields.<field>` paths, then an action stage saves the
assembled Markdown file. A Rich Live display renders a 2×1 grid showing the
field deltas as they arrive.

**Real:** model calls for positioning strategy and risk analysis, markdown file
output to `~/.agently_press_kits/`.
**Key assertions:** `selected_route=skills`, all 3 stages complete, 2,700+ char
positioning brief, 6,700+ char risk register, file saved to disk.

### 10 — Browser-Based Web Testing (local HTTP + accessibility audit)
A QA automation scenario: spin up a local HTTP server serving a sample dashboard
and settings page, then run a `web-testing` skill that browses both pages,
audits accessibility (WCAG label checks, form validation), captures a screenshot,
and compiles a test report. Demonstrates execution environment providers for
browser testing workflows.

**Real:** http.server serving HTML, httpx page fetching, BeautifulSoup HTML
parsing, PIL screenshot generation, WCAG accessibility checks.
**Key assertions:** `selected_route=skills`, 5 stages complete, accessibility
issues detected and reported, screenshot saved, structured test report generated.

### 11 — PR Severity Triage with Branch Routing (model → branch → model)
A code review scenario: analyze a realistic PR diff touching payment processing
and auth middleware. A `smart-code-review` skill uses `kind: model` for severity
triage (detects the JWT signature bypass as critical), `kind: branch` to route
to `critical`-depth review, and another `kind: model` stage that calibrates its
review depth based on the branch decision. Field deltas stream from both model
stages through `skills.stages.<id>.fields.*` paths.

**Stages:** triage_pr (model) → route_review (branch) → do_review (model) → save_review (action).
**Real:** model-driven severity classification, branch-driven review depth selection,
field-level streaming of triage reasoning and review findings.
**Key assertions:** `selected_route=skills`, 4 stages complete, severity=critical,
branch=critical routed correctly, 3,300+ char review with actionable findings.

### 12 — Incident Response with model_plan Stage (planning pipeline)
An SRE scenario: process a PagerDuty alert for payment-gateway latency with
deployment context, dependency status, and error signatures. An
`incident-response-planner` skill opens with a `kind: model_plan` stage that
generates a structured incident response plan (severity, impact radius,
mitigation, investigation steps, stakeholders, timeline), feeds it into a
`kind: model` stage for detailed runbook generation, and saves the complete
document.

**Stages:** analyze_incident (model_plan) → generate_runbook (model) → save_runbook (action).
**Real:** model_plan generation of structured incident command plan, model-driven
runbook with executable steps, field-level streaming of plan and runbook.
**Key assertions:** `selected_route=skills`, 3 stages complete, detailed incident
plan, step-by-step runbook with mitigation and investigation actions, document saved.

### 13 — Document Compliance Audit with validate + emit Stages
A legal/compliance scenario: review a vendor services agreement for regulatory
gaps. A `compliance-audit` skill extracts clauses with `kind: model`, gates
execution with `kind: validate` (halts pipeline if extraction failed), identifies
compliance gaps with a downstream `kind: model` stage, publishes the structured
audit summary via `kind: emit`, and saves the full report.

**Stages:** extract_clauses (model) → validate_extraction (validate) →
flag_compliance_gaps (model) → emit_summary (emit) → save_audit (action).
**Real:** model-driven clause extraction (8 clauses across 8 categories), validation
gating, compliance gap identification with GDPR/SOC 2 concerns, emit to runtime
stream, file output.
**Key assertions:** `selected_route=skills`, 5 stages complete, 8 clauses extracted,
validation passed, risk=HIGH identified (missing DPA, weak data handling, no
indemnification), structured audit emitted and saved.

### 14 — Deep Research Pipeline (interactive input → topic refinement → model_plan → parallel search → browse → dive → synthesis)
A comprehensive deep research scenario with interactive topic input and
2-layer research depth. A pre-pipeline Phase 0 uses a model call to expand
brief user questions (e.g., "6G什么时候商用？") into structured research briefs
with dimensions, sub-questions, and key entities. The pipeline decomposes the
topic with `kind: model_plan`, executes 3 parallel web searches via
`kind: action`, gates with `kind: validate`, browses full article content and
extracts reference hooks (companies, technologies, standards cited in articles),
runs targeted follow-up "dive" searches on those hooks, fetches dive results,
gates again with `kind: validate`, synthesizes all 3 layers (abstracts +
full-text + dive findings) with `kind: model`, cross-validates with a final
`kind: model` stage, and saves the complete research document.

**Stages (10):** plan_research (model_plan) → {search_upstream, search_core,
search_downstream} in parallel → validate_searches (validate) → browse_deepen
(action, 4 internal phases) → validate_depth (validate) → synthesize (model)
→ cross_validate (model) → compile_report (action).
**Real:** interactive topic input (CLI arg > env var > interactive prompt),
model-powered topic refinement, DuckDuckGo HTTP search, httpx article fetching
with BeautifulSoup content extraction, model-driven reference hook extraction,
model synthesis with field-level delta streaming, model cross-validation,
report output to `~/.agently_deep_research/`.
**Key assertions:** `selected_route=skills`, 10 stages complete, 3-way parallel
search fan-out, 2-layer browse+dive depth, comprehensive report with
cross-validation notes saved. No hardcoded topics — every run researches
whatever question the user asks.

### 15 — Agentic Research Pipeline (model-driven adaptive decisions)
A model-controlled research pipeline where the model decides how to research,
not just what to write. Unlike example 14's fixed pipeline (always 3 dimensions,
always 2-layer depth), here the model makes 5 key decisions during execution:
(1) how many dimensions to investigate (2–5), (2) which dimensions need
deep-dives after assessing initial results, (3) which specific articles to
browse in full (not blind top-N), (4) whether reference hooks are worth
pursuing, and (5) when enough research has been gathered — stopping early or
triggering additional gap-filling rounds. The Rich display is ordered by step
from left to right and top to bottom; queued, active, and completed panels make
the current focus explicit. Long-running research action internals update the
progress panel with real search, browse, dive, and gap-fill status, while model
decisions stream to the "Model Decisions" panel as they are made.

**Stages (6):** plan_research (model_plan, dynamic N dimensions) →
execute_research (action, agentic loop with 5 internal model calls) →
validate_coverage (validate) → synthesize (model) → cross_validate (model) →
compile_report (action).
**Real:** dynamic dimension decomposition, model conceptual depth assessment per
dimension, model article selection with reasons, model dive/skip decision,
model sufficiency check with gap detection, up to 3 adaptive research rounds,
step-ordered Rich progress and decision-log display, report output to
`~/.agently_deep_research/`.
**Key assertions:** `selected_route=skills`, 6 stages complete, model-decided
dimension count (2-5), adaptive research depth, decision log with 5+ model
decisions recorded, report reflects asymmetric depth across dimensions.

### 16 — Report Evaluation Pipeline (quality audit for research output)
A quality assurance scenario: evaluate a research report (e.g., from example 15)
against its original topic across 6 dimensions — content relevance
(signal-to-noise ratio), coverage completeness, source authority, depth
balance, internal consistency, and decision quality. Each dimension receives
a conceptual quality level with specific issues and severity ratings. Numeric
display values are mapped deterministically in code after model output.
Produces a structured evaluation matrix with cross-cutting root cause analysis
and prioritized, actionable remediation steps. Fully generic — works on any
domain's reports.

**Stages (5):** extract_metadata (model) → evaluate_dimensions (model) →
synthesize_findings (model) → generate_remediation (model) →
save_evaluation (action).
**Real:** model-driven metadata extraction, 6-dimension conceptual grading with
evidence quotes and issue tracking, code-mapped display scores, cross-cutting
root cause analysis, P0/P1/P2 remediation planning, evaluation output to
`~/.agently_deep_research/evaluations/`.
**Key assertions:** `selected_route=skills`, 5 stages complete, conceptual
levels for all 6 dimensions, severity-tagged issues, concrete remediation
recommendations.

### 17 — Self-Reflective Research Pipeline (research → evaluate → reflect → improve)
A meta-cognitive scenario: chains the agentic research core (from example 15)
with the evaluation framework (from example 16) into a self-improving loop.
The pipeline researches a topic, evaluates its own output, decides whether
reflection is needed, performs targeted gap-fill research, re-synthesizes an
improved report, and compares v1 vs v2 quality. The model controls the
reflection decision — it can ship immediately if quality is sufficient, or
trigger up to 2 reflection rounds if gaps are found.

**Stages (7):** initial_research (action, full agentic research) →
evaluate_report (model, 6-dimension conceptual grading) → reflect_and_plan
(model_plan, gap-aware reflection decision) → gap_fill_research (action,
targeted re-research) → re_synthesize (model, improved v2 report) →
final_evaluate (model, v1 vs v2 comparison) → compile_report (action).
**Real:** full agentic research with search + browse + synthesis, model
self-evaluation with conceptual levels, code-mapped display score deltas, model
reflection decision (sufficient vs. gap-fill), targeted re-research on specific
weaknesses, reflection trail documentation, report output to
`~/.agently_deep_research/`. The Rich display marks each region as queued,
active, or done; the active research panel includes elapsed time plus real
planning, search, browse, and gap-fill counters so long model calls do not look
frozen.
**Key assertions:** `selected_route=skills`, 7 stages complete, v1 and v2 levels
tracked, model decides whether to reflect, mapped delta shows improvement,
reflection trail documents the self-improvement process.
