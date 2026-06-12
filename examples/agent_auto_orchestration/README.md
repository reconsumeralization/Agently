# Agent Auto-Orchestration Examples

These examples demonstrate agent execution with real model calls and realistic
mock business data.

Three execution shapes appear here:

- **Skills examples** (01, 03, 04, 07–17) use standard `SKILL.md` packages.
  These intentionally keep their Skills on the default `single_shot` path and
  put side effects (disk writes, web fetches, scans, adaptive loops) in host
  code. The wider Skills Executor also supports `execution: staged`,
  `allowed-tools`, and `effort=`; those are demonstrated in
  `examples/skills_executor/09_runtime_planner_effort_strategy.py`.
- **Skill + real remote MCP** (18–19) shows live external MCP tools through the
  agent's Action runtime. Example 19 also proves remote public Skills declared
  on `agent.use_skills(...)` can be lazily discovered, installed, and executed
  without business-path install glue.
- **Dynamic Task DAG examples** (02, 05, 06) show submitted DAG execution:
  local handlers, model nodes, parallel branches, and field-level delta
  streaming. These do not use Skills.

**Model calls are real.** **Business data is mocked** (commit logs, CRM tickets,
product analytics, market signals) — representing what real systems would provide.

Self-authored Skills in this directory are stored as real standard Skill
directories under `examples/agent_auto_orchestration/skills/<skill-id>/SKILL.md`.
The scripts install those directories directly; they do not build inline
`SKILL.md` strings or root-level YAML manifests at runtime.

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
python examples/agent_auto_orchestration/18_amap_mcp_trip_planner.py   # needs AMAP_API_KEY
python examples/agent_auto_orchestration/19_remote_skills_weather_event_ops.py
python examples/agent_auto_orchestration/20_agent_execution_lineage_workspace_loop.py
python examples/agent_auto_orchestration/21_agent_execution_github_issue_intake.py
python examples/agent_auto_orchestration/22_unified_agent_execution_result.py
python examples/agent_auto_orchestration/23_agent_execution_auto_dispatch.py
python examples/agent_auto_orchestration/24_execution_local_dynamic_task_candidate.py
```

`_TEMPLATE_standard_skill_orchestration.py` is the canonical reference for the
single-shot Skill + host orchestration pattern.

## Skills examples (standard SKILL.md, default single_shot)

- **01 — Release Notes Generator.** From a mock commit log, one prompt-only Skill
  classifies changes, writes summaries, drafts an announcement, and assesses
  readiness; the host writes the published `.md`. Field-level streaming.
- **03 — Market Research Brief.** Mock product/analytics context is injected as
  task data; the Skill returns landscape, competitor profiles, opportunities, and
  an executive summary; the host writes the brief.
- **04 — Bilingual Lesson Plan.** Remote GarethManning education Skills produce
  a complete ZH+EN package (outlines, paired vocabulary, teacher summary); run
  twice (Chinese and English input); host writes each package.
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
  screenshot; Anthropic's remote `webapp-testing` Skill writes the QA report.
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

## Skill + real remote MCP (tool-using Skill)

- **18 — AMap MCP Trip Planner.** A two-phase remote-Skill agent. Phase 1 uses
  model-generated ActionRuntime calls against the **real remote AMap Streamable
  HTTP MCP server** via `agent.use_mcp(url)` to gather actual weather and
  points-of-interest. Phase 2 uses `ZawYePhyo/travel-planner-skill` to synthesize
  those observations into a structured one-day itinerary; the host writes the
  markdown plan. MCP data and model calls are both real. Needs `AMAP_API_KEY`
  (free at https://lbs.amap.com/); skips gracefully if absent.

- **19 — Remote Skills Weather Event Ops.** Acceptance example for the 4.1.3
  Skills runtime planner line. The business code declares public remote Skills
  (`anthropics/skills` webapp-testing, mcp-builder, docx; plus
  `davila7/claude-code-templates` computer-use-agents) only through
  `agent.use_skills(...)`. Weather facts come from the free
  `@dangahagan/weather-mcp` stdio MCP server through model-generated ActionRuntime
  calls. The selected remote Skills are materialized lazily and then executed with
  `effort="normal"` to produce a structured operations packet. Needs
  `DEEPSEEK_API_KEY`, Node.js, and `npx`; skips cleanly when prerequisites are
  missing.

- **20 — AgentExecution Lineage Workspace Loop.** Acceptance example for the
  4.1.3.7 AgentExecution lineage/limits contract. The host owns a two-step loop, runs two
  real model-backed `create_execution(lineage=..., limits=...)`
  calls, explicitly persists observations/checkpoints through the execution's
  bound Workspace helper, builds a ContextPack between steps, and verifies
  stream/meta lineage correlation.

- **21 — GitHub Issue Intake.** Business-scenario example for the 4.1.3.7
  AgentExecution lineage/limits application landing point. A DeepSeek/Ollama-backed AgentExecution receives a
  restricted bash action, decides and runs local `gh search repos`, selects the
  official repo from real command output, then runs `gh issue list` through the
  same ActionRuntime path. Host code reads AgentExecution action logs/artifact
  refs, validates the real `gh` stdout, stores the latest open issues through
  the execution's bound Workspace helper, and builds a ContextPack for
  downstream maintainer work. Runtime stall diagnostics use
  `RuntimeStageStallError`, provider stream-idle settings, and AgentExecution
  `limits.max_no_progress_seconds`; expensive RuntimeEvent outlets should use
  EventCenter hook `delivery_policy` for summary delivery when the host does not
  need token-level updates. Requires authenticated GitHub CLI.

- **22 — Unified AgentExecution Result.** Minimal real-model example for the
  unified AgentExecution expression. It uses `agent.define(...)` for reusable
  Agent definition state, a quick prompt `AgentExecutionResult` for structured
  classification, and `agent.create_task_loop(...)` as an explicit task-loop
  strategy that is still consumed through the same execution result/stream/meta
  facade.

- **23 — AgentExecution Auto Dispatch.** Minimal real-model route-selection
  example. A quick prompt execution proves the default `model_request` route,
  then an execution draft with `goal(..., success_criteria)` proves
  automatic dispatch into the task-strategy `agent_task` route. The task step
  calls a real GitHub issue-fetch Action for the first visible
  `AgentEra/Agently` issues page, the model summarizes still-pending maintainer
  work from the fetched issues, and AgentTask records observations, checkpoints,
  and verification before the example reads refs through AgentExecution
  metadata.

- **24 — Execution-Local Dynamic Task Candidate.** Infrastructure smoke for
  `execution.use_dynamic_task(...)`. It runs a submitted TaskDAG with a local
  handler, proves the `dynamic_task` route and TaskDAG stream, and verifies the
  candidate stays on the captured AgentExecution draft rather than mutating the
  Agent-level Dynamic Task candidate pool.

## Actions / Dynamic-Task DAG examples (not Skills)

### 02 — Customer Support Triage (Dynamic-Task DAG streaming)
A P1 enterprise ticket flows through four DAG nodes (classify → analyze → draft →
review) with dependency edges, each making real model calls. Mocked CRM ticket;
real urgency classification, root-cause analysis, reply drafting, QA. Nodes are
`kind="local"` callable handlers wired via `use_dynamic_task(..., handlers=...)`;
each reads its upstream inputs from `context.dependency_results[...]`.
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
