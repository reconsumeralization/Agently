---
title: Skills Executor
description: 通过 Agent API 暴露的 planner-selected declarative behavior loop。
keywords: Agently, Skills Executor, skills, behavior loop, run_skills_task, use_skills
---

# Skills Executor

> 语言： [English](../../en/development/skills-executor.md) · **中文**

Skills Executor 让 Agently 应用安装 declarative skill package，并把它们作为
planner-selected behavior loop 使用。

它和 `Agently-Skills` companion repo 不是一回事：

- **Skills Executor** 是 Agently 框架内的 runtime 能力。
- **Agently-Skills** 是给 Codex、Claude Code、Cursor 等外部 coding agent 用的伴生 guidance。

## 当前状态

这个功能正在 `feature/skills-executor` 分支实现。

当前实现提供：

- `Agently.skills_executor.install_skills(...)`、`install_skills_pack(...)`、
  `list_skills()`、`list_skills_packs()`、`inspect_skills()`、
  `inspect_skills_pack()`、`remove_skills()`、`remove_skills_pack()`
- `agent.use_skills(...)`：把可选 skill cards 披露给模型决策路径
- 对被选为候选的 SKILL.md package，按字符上限披露 primary guidance
- `agent.resolve_skills_plan(...)`：生成 `SkillExecutionPlan`
- `agent.run_skills_task(...)`：显式执行 skill task
- SkillCard 组合元数据，例如 `stage_roles`、`consumes`、`produces`、
  `artifact_types`、`side_effects`、`required_capabilities`、`complements`、
  `failure_modes`
- 通过 `semantic_outputs` 声明语义产物契约，让 realcase 测试按 deliverable
  的角色和类型验收，而不是依赖固定文件名
- 通过 `planner_mode="model"` 启用模型组合式多 skill planning，并用
  `planner_max_revisions` 控制有界 evaluate/repair loop
- declarative `action`、`model`、`validate`、`emit` stage 处理
- `run_skills_task(...)` 底层使用 Dynamic Task DAG 执行，
  `SkillExecution.close_snapshot` 会保留编译后的 task graph 结果以及 skill/action logs

当前实现把 model-owned planning 放在 `SkillExecutionPlan` contract 后面。Planner
可以选择和组合多个候选 skills，描述阶段切换、approval gate、fallback 路径、
中间产物、side effect 边界，以及预期语义产物。

保留的框架层次是：

```text
core/SkillsExecutor.py
  -> active SkillsExecutor plugin
  -> builtins/plugins/SkillsExecutor/
  -> builtins/agent_extensions/SkillsExtension.py
```

`Agently.skills_executor` 是这个开发线功能唯一的全局 facade。该功能尚未发布，
因此不保留 `Agently.skills` 兼容别名。

## 用户心智模型

Skill 不是 `skill.run()` 函数，也不是 `ActionExecutor`。

```text
Agent API
  -> skill cards and policy filtering
  -> SkillExecutionPlan
  -> Dynamic Task DAG
  -> SkillExecution
  -> Actions for atomic work
```

Action 仍然是原子能力。Skill 负责把这些能力组织成一个行为 loop。

## 可选 Skills

普通 agent 请求中，只想把 skills 作为候选能力时，用 `use_skills(...)`。

```python
agent = Agently.create_agent("ops-assistant")

agent.use_skills(
    ["release-checklist", "incident-triage"],
    mode="model_decision",
)

response = await (
    agent
    .input("Should this production issue trigger rollback?")
    .get_response()
    .async_get_text()
)
```

这会披露 skill cards，但不会强制执行某个 skill。
对于 SKILL.md package，primary guidance 正文也会按每个 skill 的字符上限进入 prompt，
让模型能使用 checklist 细节，但不会自动读取或执行 scripts/resource 目录。

同样支持 Agently 原有链式风格。安装后的 skills 类似 actions/tools 一样作为
model-decision 能力被装载进请求，由模型判断是否适用。

```python
result = (
    agent
    .use_skills(["release-checklist"])
    .input("Should this release be blocked?")
    .instruct("Use installed skills only if they fit the task.")
    .output({"reply": (str,)})
    .start()
)
```

## 强制 Skill Task

当任务必须通过 skill loop 完成时，用 `run_skills_task(...)`。

```python
execution = await agent.async_run_skills_task(
    "prepare release notes",
    skills=["release-checklist"],
    mode="required",
)

print(execution.status)
print(execution.output)
print(execution.action_logs)
```

`required` 模式下，请求的 skills 必须被选中。缺依赖、权限被拒或 Action 不可用时，
应 fail closed。

对于组合型 Skill Pack，传入预期 deliverables 作为语义契约，并让模型 Planner
组合完整行为 loop：

```python
execution = await agent.async_run_skills_task(
    "Design a 4-week B1 business English course package.",
    skills=[
        "learner-profile-intake",
        "backwards-design-unit-planner",
        "retrieval-practice-generator",
        "formative-assessment-generator",
        "docx",
        "pdf",
        "pptx",
        "xlsx",
    ],
    mode="model_decision",
    semantic_outputs=[
        "course_plan.json",
        "teacher_guide.docx",
        "student_handout.pdf",
        "lesson_slides.pptx",
        "progress_tracker.xlsx",
        "skill_trace.json",
    ],
    planner_mode="model",
    planner_max_revisions=2,
)

print(execution.plan["selected_skills"])
print(execution.plan["stage_plan"])
print(execution.plan["planner_evaluation"])
print(execution.close_snapshot["task_dag"]["semantic_outputs"])
```

`semantic_outputs` 可以直接传类似文件名的字符串，也可以传显式 deliverable dict。
执行器会把它们归一化为 role 和 artifact type，所以只要计划覆盖了要求的语义产物，
文件名不同也不应误判失败。文件名规范化是执行器职责，不是用户契约。

## 最小 Skill Package

```yaml
skill_id: release-checklist
display_name: Release Checklist
purpose: Check release readiness and record a release note.
trust_level: local
activation:
  keywords: [release, rollback]
requires:
  actions: [record_release_note]
stages:
  - id: record_note
    kind: action
    action: record_release_note
    input:
      text: "${task}"
  - id: validate_note
    kind: validate
    validation:
      required_state: [record_note]
```

## 组合元数据

真实 Skill Pack 应说明它和其他 Skills 如何组合。这些字段会保存在 `SkillCard`
里，并披露给 planner。

```yaml
card:
  stage_roles: [intake, action, validation]
  consumes:
    - role: task_request
      type: text
  produces:
    - role: release_note
      type: json
  artifact_types: [json]
  side_effects:
    - kind: local_record
      policy: allowed
  required_capabilities: [record_release_note]
  complements: [repo-review]
  failure_modes: [missing_action]
```

## 边界

- scripts/helpers 必须通过受控 Actions 执行，不能作为任意 Skill Python handler。
- 第三方 Skill scripts 默认作为 inert assets 安装，但能力解析属于执行器职责。执行器
  应先尝试受控替代方案，例如内置 Actions、sandbox-backed Bash/Python/Node actions、
  MCP/API bindings，或声明式 fallback branches，再决定阻塞执行。
- 如果 Skill 需要 Bash/shell 类型 action 但应用没有绑定，执行器可以按配置自动绑定
  受控 Bash sandbox，并使用命令 allowlist 和 workspace boundary。它不能静默执行
  第三方包里的任意 scripts。
- 如果找不到受控替代方案，执行结果应 fail closed，并返回自然语言 `user_message`
  和修复建议，而不是让业务代码只能解析内部错误码。
- guidance 披露只是 prompt context，不是代码执行。
- MCP、browser、sandbox、process、credentials 等资源生命周期归 Execution Environment。
- 长跑 workflow 行为应通过 TriggerFlow-backed skill execution 表达，不应藏在 Skill package 内部。

## 已实测外部 Skill 包

`examples/skills_executor/02_deepseek_external_skill_cards.py` 会安装并测试：

- `../Agently-Skills/skills/agently-runtime`
- `anthropics/skills/skills/xlsx`
- `anthropics/skills/skills/webapp-testing`

这个 example 验证 DeepSeek 在 `model_decision` 模式下能收到选定 skill 的 card 和受限
primary guidance；包内 scripts 仍然只是资产，除非应用把它们绑定为受控 Actions，否则不会执行。

`examples/skills_executor/04_dynamic_todo_triggerflow_realcase.py` 是 diagnostic realcase
版本。它安装 `Agently-Skills`，通过 `agent.use_skills(...)` 把 `agently-playbook`、
`agently-request`、`agently-triggerflow` 披露给 DeepSeek，然后让 DeepSeek 同时生成
Todo DAG 和完整 Python TriggerFlow executor module。prompt 不硬写 TriggerFlow API 细节；
host 脚本只评估模型生成模块是否用了真实 Agently API，以及能否跑通。这个 diagnostic
默认把 pass/fail 作为数据打印并以 0 退出，便于交互式运行；CI gate 可传
`--strict-exit`，让评估失败返回非 0。

`examples/skills_executor/03_stock_research_business_minimal.py` 是业务视角最简样例。
它通过 `Agently.skills_executor.install_skills_pack(..., name="equity-research-demo")`
安装本地 Skill Pack，再通过 `agent.use_skills_packs(...)` 挂载 pack，最后把股票
研究任务交给 DeepSeek 或本地 Ollama。在模型分析前，执行器会先运行受控
`fetch_equity_market_data` Action stage，从 Stooq CSV endpoint 拉取当前公开报价。
provider timestamp 可能有延迟，并不是交易所直连 realtime tick，但结果是在运行时
真实获取的，不再使用 sample data。503/504 和 timeout 会先重试；如果 provider 仍然
不可用，Action 会降级使用最近一次成功的本地 quote cache，并把 data status 标记为
degraded；如果没有 cache，则把对应 ticker 标记为 unavailable。

`examples/skills_executor/05_combo_skillpack_diagnostics.py` 是组合 Skill Pack 基准样例，
覆盖：

- 英语教学课程包生成
- 股票研究材料包生成
- 旅行规划，以及写入外部工具前的人类确认边界
- 调研报告到表格、文档、PPT、PDF
- Web 应用验收测试证据包

组合基准使用本地真实 `SKILL.md` 包；如果公共 Skill Pack 源码缺失，它会显式跳过
对应 case，而不是用 mock skill 替代。传入 `--fetch-missing` 可以拉取这些公开仓库。
现在基准会通过 `agent.run_skills_task(..., semantic_outputs=...,
planner_mode="model")` 运行。模型只会看到可选 SkillCards 和受限长度的主 guidance；
执行器会把模型组合出的计划转换为 Dynamic Task DAG；host 评估器检查 skill 选择、
阶段切换、中间产物、side effect 边界、approval gate、fallback 和语义输出覆盖率。

完整 DeepSeek benchmark 会在确定性 gate 之后再跑一次 Agently 模型 judge 做内容级判断。
judge 的 output schema 把 evidence 和简短 reason 放在每条规则的最终布尔字段之前，
并把整体 `passes` 布尔字段放在最后，让最终判断受前面结构化信息支撑。

这个基准是 plan/contract 和 Dynamic Task 执行层的验收门槛。它不声称第三方文档
skills 的任意脚本已经被执行，也不声称已经真实写出了 `.docx`、`.pdf`、`.pptx`
或 `.xlsx` 文件。真实副作用必须由受控 Actions 和 Execution Environment 绑定提供。

`examples/skills_executor/06_executable_education_course_pack.py` 是第一个真实执行层
benchmark。它复用外部 Skill Pack 的规划路径，然后通过 Skills Executor 运行一个本地
dependency-installer Skill。该 Skill 会调用受控 `ensure_python_packages` Action，
在生成文件前安装缺失的 artifact writer 依赖，例如 `python-docx`、`openpyxl`、
`python-pptx`、`reportlab`、`pypdf`。本地库缺失不是自然降级条件；必须由 Action
修复，修复失败则 fail closed。依赖修复后，benchmark 会真实写出 `docx`、`pdf`、
`pptx`、`xlsx` 和 `json` 文件，做确定性文件校验，并用 output-controlled Agently
model judge 做内容语义判断。

同样的五个组合 case 也已经注册为 pytest benchmark：

```bash
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_combo_benchmarks.py

AGENTLY_RUN_SKILLS_BENCHMARKS=1 \
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_combo_benchmarks.py -m skills_benchmark

AGENTLY_RUN_SKILLS_REAL_EXECUTION=1 \
PYTHONPATH=. python -m pytest -q tests/test_skills_executor_real_execution_benchmarks.py -m skills_real_execution
```

第一条命令只验证 source discovery 和安装，不调用模型。第二条命令会运行完整
DeepSeek planning benchmark。第三条命令是真实执行 benchmark，应作为
artifact-producing Skills 的验收门槛。

## 相关文档

- [Coding Agents](coding-agents.md)
- [Action Runtime](../actions/action-runtime.md)
- [Execution Environment](../actions/execution-environment.md)
- [TriggerFlow Overview](../triggerflow/overview.md)
