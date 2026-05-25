---
title: Agently 4.1.3 Release Notes
description: 从 4.1.2 运行时基础到 4.1.3 AI 应用运行时主线的 Agently 4.1.3 release note。
keywords: Agently, release notes, 4.1.3, Agent, Skills Executor, Dynamic Task, MCP
---

# Agently 4.1.3 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.md) · **中文**

Agently 4.1.3 是 4.1.2 运行时基础正式连成 AI 应用运行时的一版。

这一版的重点不是再列一组新增 API，而是让一次 Agent turn 可以把模型推理、
Actions、远程 Skills、MCP 工具、Dynamic Task DAG、运行过程流、结构化输出和
companion coding-agent 指引放进同一条工程路径里。

4.1.2 建立了运行时基础能力。4.1.3 把这些基础能力连接成默认应用路径，使
Agently 可以支撑真实 AI 服务，而不只是 prompt demo。

## 核心结果

Agently 现在可以作为生产级 AI 服务后端的执行基座：

```text
业务输入
  -> Agent
  -> 候选 Actions / Skills / Dynamic Task
  -> 模型参与规划和执行
  -> ActionRuntime / ExecutionEnvironment / TriggerFlow
  -> 流式过程事件
  -> 结构化业务输出
```

真实 AI 服务需要的不只是文本生成。它们需要稳定的输出契约、可观测的工具调用、
外部系统边界、可恢复的执行过程，以及让开发者和 coding agent 都能遵循的当前
推荐路径。

## Agent 成为默认运行时入口

`agent.start()` 现在是一次候选能力感知 Agent turn 的默认用户层入口。调用方仍然
拿到业务结果；当显式声明了候选能力时，Agent 可以路由到普通模型响应、Actions、
Skills Executor 或 Dynamic Task。

```python
result = (
    agent
    .use_actions([lookup_customer, fetch_contract, notify_owner])
    .use_skills(
        [{"source": "anthropics/skills", "subpath": "skills/docx"}],
        mode="model_decision",
    )
    .use_dynamic_task(mode="auto", max_tasks=8)
    .input({"customer_id": "C-1024", "ticket": "payment failure"})
    .output({
        "summary": (str, "business summary", True),
        "risk_level": (str, "low / medium / high", True),
        "next_actions": ([str], "recommended actions", True),
    })
    .start()
)
```

业务价值：应用代码只需要描述一次业务任务可用的能力，运行时负责选择和执行合适
路径，而不是把服务写成手工 prompt 拼接。

## Execution Object 和过程流

需要进度、诊断、日志或前端流式展示的服务，可以把同一个 Agent turn 创建为
execution object。

```python
execution = (
    agent
    .use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)
    .input({"ticket": "T-42"})
    .create_execution()
)

async for item in execution.get_async_generator(type="instant"):
    send_to_ui(item.path, item.value)

data = await execution.async_get_data()
meta = await execution.async_get_meta()
```

业务价值：前端和日志系统不再只能显示黑盒 loading。它们可以展示路由决策、图就绪、
任务开始、Action 调用、字段增量、blocked 状态和最终结构化输出。

## Skills 成为运行时能力

`agent.use_skills(...)` 现在是推荐的 Agent 级 Skills 声明入口。业务代码声明候选
source；Skills Executor 负责轻量发现、规划选择、按需 materialize、能力挂接、
诊断和执行。

```python
agent.use_skills(
    [
        {"source": "GarethManning/education-agent-skills"},
        {"source": "anthropics/skills", "subpath": "skills/docx"},
        {"source": "anthropics/skills", "subpath": "skills/pptx"},
        {"source": "anthropics/skills", "subpath": "skills/xlsx"},
    ],
    mode="model_decision",
)

execution = await agent.async_run_skills_task(
    "Create a four-week B1 business English course package.",
    effort="normal",
    output={
        "course_plan": (dict, "course goals, weekly structure, and lesson sequence", True),
        "teacher_guide": (str, "teacher-facing guide summary", True),
        "student_handout_plan": (str, "student material plan", True),
        "progress_tracker": ([str], "progress tracking columns and checkpoints", True),
    },
)
```

业务价值：Skills 不再是业务代码里内联的 prompt 片段，而是可复用的运行时能力。
团队可以指向公开或私有 Skill 仓库，保留本地目录作为开发方式，并让运行时只安装
当前任务真正需要的资源。

## MCP 和脚本能力走运行时边界

包含 MCP、shell 或脚本声明的 Skills 由 Skills Executor 解释，再通过已有
ActionRuntime 和 ExecutionEnvironment 边界挂接。Skill 不会变成第二套工具系统。

```python
agent.use_skills(
    [{"source": "OctagonAI/skills", "trust_level": "remote"}],
    mode="required",
    auto_allow=False,
)

await agent.use_mcp({
    "mcpServers": {
        "market_data": {
            "command": "npx",
            "args": ["-y", "octagon-mcp"],
        }
    }
})
```

HTTP MCP 服务可以直接使用：

```python
await agent.use_mcp(
    "https://example.com/mcp",
    headers={"Authorization": "Bearer ..."},
)
```

业务价值：外部工具、本地命令和 MCP 服务都成为可观测、受策略控制的运行时能力。
高风险本地执行需要显式审批或 `auto_allow=True`；缺失的安全纯计算能力可以合成为
sandboxed Python action；业务系统能力如果没有真实 Action 或 connector，则会
fail closed。

## Effort-aware Skills Planning

Skills 执行现在支持运行时 effort 等级和自定义 effort strategy handler。

```python
execution = await agent.async_run_skills_task(
    "Prepare release readiness evidence and decide go/no-go.",
    skills=["release-readiness-reviewer"],
    mode="required",
    effort="normal",
    output={
        "decision": (str, "go / no-go", True),
        "blocking_risks": ([str], "release blocking risks", True),
        "required_followups": ([str], "follow-up actions", True),
    },
)
```

Effort 语义：

- `fast`：在保证任务完成的前提下压缩规划和复核环节。
- `normal`：完整经过 preflight、research/context、plan、execute、verify、
  reflect/retry、finalize。
- `max`：使用更高预算、更强校验和重试回环，复杂任务可进一步走向 Dynamic Task
  DAG 执行。

团队也可以注册自己的策略：

```python
Agently.skills_executor.register_effort_strategy("audit_plus", handler)

execution = await agent.async_run_skills_task(
    "Run a regulated readiness review.",
    effort="audit_plus",
)
```

业务价值：团队可以显式权衡延迟、成本和可靠性。同一个 Skill 可以用于日常快速任务、
重要业务决策，或组织自定义的高保证流程。

## Dynamic Task 和 TriggerFlow 成为执行骨架

复杂的模型生成 DAG 或应用提交 DAG 仍然由 Dynamic Task 承载。4.1.3 让 Agent
execution 可以路由到 Dynamic Task，并把结构化字段增量保留在稳定路径下。

```python
execution = (
    agent
    .use_dynamic_task(mode="auto", max_tasks=8)
    .input("Research this company and produce an investment memo.")
    .output({
        "thesis": (str, "investment thesis", True),
        "risks": ([str], "major risks", True),
        "evidence": ([str], "supporting evidence", True),
    })
    .create_execution()
)

async for item in execution.get_async_generator(type="instant"):
    if item.delta:
        print(item.path, item.delta)
```

业务价值：复杂任务可以在同一运行时中分解、流式展示、检查和恢复，而不是塞进一次
prompt，或重写成另一套工作流引擎。

## Model Pool 阶段路由

Skills 规划和执行阶段使用 model key，而不是硬编码 provider model name。

```python
Agently.set_settings(
    "skills.runtime.stage_model_keys",
    {
        "planner": "reason",
        "research": "research",
        "executor": "executor",
        "verifier": "reason",
        "finalizer": "executor",
    },
)
```

业务价值：服务可以把不同阶段路由给便宜、快速或更强的模型，而不需要改业务代码。
规划、调研、执行、校验、反思和最终生成都能使用适合该阶段的模型。

## 推荐服务类型

4.1.3 特别适合：

- 企业运营服务：工单分流、事故响应、续约风险、销售研究；
- 研究和报告服务：市场分析、政策摘要、开源项目评估、投研 memo；
- 专业 artifact 工作流：一次结构化运行生成 docx、xlsx、pptx、pdf；
- 外部工具 Agent：MCP、数据库、浏览器、计算器、本地执行、业务 connector；
- 需要前端过程可视化的长流程 AI 服务。

核心变化是 Agently 现在提供了一套统一运行时心智：

```text
声明能力
用模型规划
通过已有运行时边界执行
流式展示过程
返回结构化业务结果
```

## 兼容性信息

- 包版本为 `4.1.3`。
- Release manifest 为 `compatibility/releases/4.1.3.json`。
- Agently 4.1.3 推荐 `agently-devtools >=0.1.5,<0.2.0`。
- Agently-Skills 使用 authoring protocol `agently-skills.authoring.v2` 和标准
  `SKILL.md` 包。
- Skills execution 的 `semantic_outputs=` 保留为 deprecated compatibility alias。
  新代码应使用 `output=`。

