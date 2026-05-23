# Agent Auto-Orchestration Examples

These examples demonstrate agent execution with real model calls and realistic
mock business data.

Two execution shapes appear here:

- **Skills examples** (01, 03, 04, 07–17) use the **standard `SKILL.md`** model:
  a Skill is guidance only (no `skill.yaml`, no stages, no embedded actions).
  Running it is a single prompt-only request; structured output is shaped with
  `semantic_outputs=`. Side effects (disk writes, network/tool calls) and any
  multi-step or iterative orchestration live in **host code** — registered
  Actions, host functions, and host loops. Configure the registry with the public
  `Agently.skills_executor.configure(registry_root=..., allowed_trust_levels=[...])`.
- **Actions / Dynamic-Task DAG examples** (02, 05, 06) show TriggerFlow /
  Dynamic Task DAG execution with `kind="model"` nodes, parallel branches, and
  field-level delta streaming. These do not use Skills.

**Model calls are real.** **Business data is mocked** (commit logs, CRM tickets,
product analytics, market signals) — representing what real systems would provide.

Run from the repository root (`DEEPSEEK_API_KEY` in env or `.env`; set
`DYNAMIC_TASK_MODEL_PROVIDER=ollama` for a local Ollama endpoint):

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
python examples/agent_auto_orchestration/09_multi_skill_model_orchestration.py
python examples/agent_auto_orchestration/10_browser_web_testing.py
python examples/agent_auto_orchestration/11_branch_code_review.py
python examples/agent_auto_orchestration/12_model_plan_incident_response.py
python examples/agent_auto_orchestration/13_validate_emit_compliance_check.py
python examples/agent_auto_orchestration/14_deep_research.py
python examples/agent_auto_orchestration/15_agentic_research.py
python examples/agent_auto_orchestration/16_report_evaluation.py
python examples/agent_auto_orchestration/17_self_reflective_research.py
```

`_TEMPLATE_standard_skill_orchestration.py` is the canonical reference for the
new pattern (Skill = guidance, host = orchestration via TriggerFlow).

## Skills examples (standard SKILL.md, prompt-only)

- **01 — Release Notes Generator.** From a mock commit log, one prompt-only Skill
  classifies changes, writes summaries, drafts an announcement, and assesses
  readiness; the host writes the published `.md`. Field-level streaming.
- **03 — Market Research Brief.** Mock product/analytics context is injected as
  task data; the Skill returns landscape, competitor profiles, opportunities, and
  an executive summary; the host writes the brief.
- **04 — Bilingual Lesson Plan.** One Skill produces a complete ZH+EN package
  (outlines, paired vocabulary, teacher summary) in one pass; run twice (Chinese
  and English input); host writes each package.
- **07 — Code Security Audit.** The **host** runs deterministic scanners (regex
  secret/injection detection, CVE lookup) first; a prompt-only Skill triages the
  raw findings into a prioritized audit report; host writes it.
- **08 — Web Research Report.** The **host** does the real web search + page fetch
  (httpx/DuckDuckGo, with a simulated fallback); a prompt-only Skill synthesizes
  the fetched sources into a cited report; host writes it.
- **09 — Product Launch Press Kit.** One Skill returns a positioning brief + risk
  register (field-streamed); host writes the press kit.
- **09_multi — Multi-Skill Composition.** Three Skills are installed; with
  `mode="model_decision"` the planner has the model select/order them, then the
  executor runs one prompt-only request that synthesizes all selected guidance.
- **10 — Browser Web Testing.** The **host** serves a local app, runs a
  deterministic accessibility scan, and (if Playwright is installed) captures a
  screenshot; a prompt-only Skill writes the QA report.
- **11 — Smart Code Review.** One Skill triages PR severity and produces a review
  whose depth scales with severity (replacing the old `branch` stage); host saves
  the review.
- **12 — Incident Response Planner.** One Skill produces a response plan + on-call
  runbook from a PagerDuty alert; the host persists the document (the old `action`
  stage is now host code, where approval/wait policy belongs).
- **13 — Document Compliance Audit.** One Skill extracts clauses, flags missing/
  weak categories and gaps, and emits a risk summary (replacing `validate`+`emit`
  stages); host writes the report.
- **14 — Deep Research.** One prompt-only Skill produces a deep, multi-dimension
  report with synthesis and open questions; host saves it.
- **15 — Agentic Research.** The **host** runs an adaptive loop (up to 3 rounds):
  each round the Skill returns a report + a sufficiency judgement + remaining
  gaps; the host decides whether to run another round, feeding gaps back in. The
  model can stop early.
- **16 — Report Evaluation.** One Skill grades a report across six dimensions
  (EXCELLENT/ADEQUATE/WEAK/FAILED) with issues + verdict; the host maps levels to
  a numeric score. Evaluates a built-in sample, a file path, or `AGENTLY_EVAL_REPORT`.
- **17 — Self-Reflective Research.** The **host** runs a reflect→revise loop (up to
  2 revisions): the Skill drafts, then critiques and improves prior drafts and
  judges whether further revision is warranted; the host keeps the reflection trail.

## Actions / Dynamic-Task DAG examples (not Skills)

### 02 — Customer Support Triage (Actions + DAG streaming)
A P1 enterprise ticket flows through four DAG nodes (classify → analyze → draft →
review) with dependency edges, each making real model calls. Mocked CRM ticket;
real urgency classification, root-cause analysis, reply drafting, QA.
Key assertions: `selected_route=dynamic_task`, four task stream events.

### 05 — Operator-visible Field Delta Streaming
A submitted Dynamic Task DAG with `kind="model"` nodes; the CLI filters paths like
`task_dag.tasks.reply.fields.reply` and prints `item.delta` so notes and reply
text appear while the model is still generating.

### 06 — Parallel DAG Field Delta Streaming (multi-branch)
Three independent workstreams (Security, Performance, UX) run concurrently after a
shared context step; a final executive brief fans in for a go/no-go verdict.

```
context ──┬── security_analysis ── security_signoff ──┐
          ├── perf_analysis ─────── perf_signoff ─────┤
          └── ux_analysis ───────── ux_signoff ───────┘
                                      executive_brief ←┘
```
