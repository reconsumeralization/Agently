---
title: Skills Executor
description: 通过 Agently 安装和执行标准 SKILL.md Skill。
keywords: Agently, Skills Executor, SKILL.md, skills, run_skills_task, use_skills
---

# Skills Executor

> 语言： [English](../../en/development/skills-executor.md) · **中文**

Agently Skills 遵循标准 Skills 目录：`SKILL.md` 是能力定义，`scripts/`、
`references/`、`assets/` 是可选资源目录。Agently 不定义额外的 Skill
作者清单。

```markdown
---
name: release-review
description: Use when checking release readiness and rollback risk.
---

# Release Review

Follow this checklist before recommending a release or rollback...
```

## 声明和安装

正常 Agent 运行时，优先在 Agent 上用 `use_skills(...)` 声明 Skills。
Skills Executor 会像 Action candidate 一样先记录 source，在规划时轻量发现
`SKILL.md`，只有 planner 选中或 required 时才完整安装 Skill 和资源。

```python
agent.use_skills(
    [{"source": "anthropics/skills", "subpath": "skills/docx"}],
    mode="required",
)
```

`install_skills_pack(...)` 保留为高级池管理入口：预热、离线镜像、确定性 CI
fixture、显式 registry 维护。`install_skills(...)` 仍用于单个本地 Skill
目录的作者开发和 smoke test。

自己编写本地 Skill 时，也应放在完全独立、符合标准结构的目录中。业务代码不要
拼 inline `SKILL.md` 字符串，也不要使用根目录 `skill.yaml` 这类 YAML 清单；
应用层只把目录路径交给 executor：

```text
my-skill/
|-- SKILL.md
|-- scripts/
|-- references/
`-- assets/
```

```python
report = Agently.skills_executor.install_skills_pack(
    "anthropics/skills",
    fetch=True,
    subpath="skills/docx",
    trust_level="remote",
)
```

远程安装会把仓库 clone 到 Agently 的本地 registry source cache，再把标准
`SKILL.md` 包复制进 registry，并记录 source URL、ref、解析后的 commit、
subpath、trust level 和 checksums。安装远程 Skill 不会执行包内 scripts。

`install_skills(...)` 会把标准本地 Skill 目录复制到本地 registry。安装后的
Skill 根目录仍然直接包含 `SKILL.md`。Agently 只在安装副本内添加 `.agently/`
管理目录。

```text
.agently/skills/release-review/
|-- SKILL.md
|-- scripts/
|-- references/
|-- assets/
`-- .agently/
    |-- install.json
    |-- decision_card.json
    |-- resource_index.json
    `-- checksums.json
```

`.agently/` 文件用于加速路由、检查和资源索引，不是 Skill 能力定义。派生文件缺失或过期时，Agently 会重建，或直接回退读取 `SKILL.md`。

`skill_id` 由 `SKILL.md` frontmatter 里的 `name` 派生：小写、空白变成
`-`，只保留 `a-z0-9._-`。后续调用建议使用安装返回的
`contract["skill_id"]`。

```python
contract = Agently.skills_executor.install_skills("./release-review")
agent.use_skills([contract["skill_id"]], mode="model_decision")
```

根目录下的 `skill.yaml`、`skill.json`、`agently.skill.yaml` 等非标准清单会被拒绝。`scripts/`、`references/`、`assets/` 里的同名文件只作为普通资源处理。

## 选择

用 `use_skills(...)` 将已安装或远程 Skills 暴露为 route candidates。模型先看到简短 decision cards；只有 Skills route 真正执行时才 materialize 完整 guidance 和资源。

```python
agent = Agently.create_agent("ops-assistant")
agent.use_skills(["release-review"], mode="model_decision")
agent.use_skills([{"source": "anthropics/skills", "subpath": "skills/docx"}], mode="required")
```

需要检查将会使用哪些 Skills 时，调用 `resolve_skills_plan(...)`。Required
Skills 保持调用方顺序；多个可选候选由模型排序。

```python
plan = await agent.async_resolve_skills_plan(
    "Should this release be blocked?",
    skills=["release-review", "incident-triage"],
    mode="model_decision",
)
```

## 执行

当任务必须通过 selected Skills 回答时，用 `run_skills_task(...)`。默认执行策略是
`single_shot`：Agently 会把 selected Skills 的 `SKILL.md` guidance、
decision cards、资源摘要和任务放进一次模型请求。多步策略由宿主侧执行选项
选择，例如 `effort="react"` 或 route options；Skill 不能通过 Agently 私有
frontmatter 声明执行策略。
当可用 action 存在时，`react` 会把 tool/action 规划和执行委托给 Agent
ActionRuntime，因此 kwargs schema、MCP tools、policy、approval、concurrency 和
Execution Environment 处理仍由 Action 层拥有，而不是由 Skills 重新实现。

```python
execution = await agent.async_run_skills_task(
    "Review this release and give a go/no-go recommendation.",
    skills=["release-review"],
    mode="required",
)

print(execution.status)
print(execution.output)
print(execution.skill_logs)
```

`output=` 使用和 `.output(...)` 相同的 schema grammar；它就是本次
Skill run 的结构化输出契约，描述 Skill 执行要交付的业务结果形状。
`output_format=` 才控制承载格式，例如 JSON、flat Markdown、hybrid、XML-like field envelope、YAML literal 或自动选择。
旧的 `semantic_outputs=` 参数仅作为 Skills 执行的兼容别名保留，并会触发
deprecation warning。

```python
execution = await agent.async_run_skills_task(
    "Write a release decision.",
    skills=["release-review"],
    mode="required",
    output={"decision": (str, "go or no-go", True)},
)
```

显式 Skills 执行也支持 Agent prompt 方法。Skill run 会消费当前 prompt snapshot，
把渲染后的 prompt 文本作为 task，并把 `output` / `output_format` slot 映射为
`output` / `output_format`：

```python
execution = await (
    agent
    .info({"release": "4.1.2.x"})
    .input("Write a release decision.")
    .output({"decision": (str, "go or no-go", True)}, format="json")
    .async_run_skills_task(skills=["release-review"], mode="required")
)
```

`set_agent_prompt(...)` 写入的长期 prompt 会被继承并保留给后续 execution；
`set_turn_prompt(...)`、兼容别名 `set_request_prompt(...)` 和 quick prompt 写入的
本轮 execution prompt 会被冻结到这次 Skill run，然后从 pending execution prompt 清理。显式传入的 `output=` 和
`output_format=` 参数优先于 prompt 推导值。

`output_format=` 用于选择这次模型响应的输出控制方式。普通 Skill 回答保持默认
`"auto"`。Auto 是结构规则：扁平且全是字符串字段时选择
`"xml_field"`；顶层 dict 同时包含字符串字段和任意非字符串 typed 字段时选择
`"hybrid"`；全控制字段、全复杂结构和非 dict 输出选择 `"json"`。紧凑全 typed
机器可读结果或下游 JSON-only 契约应显式用 `"json"`。扁平纯字符串字段
适合 XML-like field boundary 时可显式用 `"xml_field"`；长文本混合 typed
字段时可显式用 `"hybrid"`；只有明确需要 YAML target document 时才显式用
`"yaml_literal"`。

```python
execution = await agent.async_run_skills_task(
    "Draft a release announcement as HTML.",
    skills=["release-review"],
    mode="required",
    output={"html": (str, "render-ready HTML", True)},
    output_format="xml_field",
)
```

固定必填字段优先写在 schema 元组第三项：

```python
output = {
    "rules": [
        {
            "rule_id": (str, "Stable rule id", True),
            "passed": (bool, "Whether this rule passed", True),
            "evidence": (str, "Concise evidence; empty string is allowed", False),
        }
    ],
    "passes": (bool, "Overall pass/fail", True),
}
```

运行时 `ensure_keys=` 只用于条件路径或运行时才决定的路径。`max_retries=3`
表示解析失败、必填 key 缺失、严格输出校验失败或自定义 validator 失败时，Agently
最多还会发起三次额外模型尝试。普通遗漏、markdown header 错误、auto format
降级到 JSON 通常能靠重试恢复；但模型持续回显占位符脚手架、用散文填布尔/数字字段、生成畸形嵌套数组、长 prompt 被截断，或需要填很多
`rule_results[*].evidence` 这类 wildcard 路径时，三次重试后仍可能失败。多规则
model judge 建议显式 `output_format="json"`，schema 尽量浅，规则过多时拆成多次
judge。

直接执行 Skills 时，`stream_handler` 会收到 runtime items：

- `skills.prompt_only.start`
- `skills.model_stream`，包含 `path`、`value`、`delta`、`is_complete`
- `skills.prompt_only.done`
- `effort="normal"` 或 `effort="max"` 选中内置 planner chain 时，会收到
  `skills.runtime_chain.*`
- 选中多步策略时，还会收到 `skills.staged.*`、`skills.react.*` 和 `block.*`
  事件

直接 Skills `stream_handler` 回调可用 `agently.types.data` 里的
`SkillRuntimeStreamHandler` 标注。如果你在自定义 Skills effort strategy 里调用
`context.async_request_model(..., stream_handler=...)`，这个模型流回调收到的是
`StreamingData`，可用 `ModelStreamingHandler` 标注。两个类型都可以从根入口导入：
`from agently import StreamingData, ModelStreamingHandler`。

`effort="fast"` 使用低开销 single-shot 路径。`effort="normal"` 固定走完整
preflight -> research -> plan -> execute -> verify -> reflect -> finalize
链路。`effort="max"` 使用同一链路，但提高 retry 预算，并作为后续 Dynamic Task
升级的挂接点。

需要覆盖内置档位时，可以用 `agent.set_settings("effort_presets", {...})` 把调用方
看到的质量/成本档位映射到策略、model key、step budget、retry count 和 artifact
inline limit：

```python
agent.set_settings("effort_presets", {
    "fast": {"strategy": "single_shot", "reason_key": "reason_fast", "step_budget": 1},
    "normal": {"strategy": "runtime_chain", "reason_key": "reason", "retry_count": 1},
})

execution = await agent.async_run_skills_task(
    "Draft a release decision.",
    skills=["release-review"],
    mode="required",
    effort="normal",
)
```

当 Skills 由 Agent auto-orchestration 自动选中时，通过
`create_execution(options=...)` 传入同一个 effort 选择：

```python
from agently.types.options import ExecutionOptions, SkillsRouteOptions

execution = agent.input("Draft a release decision.").create_execution(
    options=ExecutionOptions(
        routes={"skills": SkillsRouteOptions(effort="normal")},
    )
)
```

需要完全自定义行动策略时，可以在 Skills Executor 上注册 effort strategy handler，
再通过 `effort=` 调用。handler 会拿到 Agent runtime context、选中的 Skills plan、
解析后的 effort config 和 output format；它可以请求模型、通过 context 调用
Action/MCP、发 runtime stream，并返回最终输出。

handler 遵循 `SkillsEffortStrategyHandler` protocol：

```python
def handler(
    *,
    context: SkillsExecutionContext,
    task: str,
    plan: SkillExecutionPlan,
    output_format: str | None = None,
    effort: str | None = None,
    effort_config: dict | None = None,
) -> Awaitable[Any] | Any: ...
```

内置 handler 也注册在同一张 strategy 表里：`single_shot`、`runtime_chain`、
`staged` 和 `react`。可以用
`Agently.skills_executor.list_effort_strategies()` 查看当前可用策略名。自定义
handler 只有显式传入 `replace=True` 时才能替换内置策略；否则重名会 fail closed。
内置参考实现位于
`agently/builtins/plugins/SkillsExecutor/AgentlySkillsExecutor/modules/effort_strategies/`。

```python
async def audit_plus_strategy(*, context, task, plan, effort_config, **_):
    await context.async_emit_runtime_stream({
        "type": "skills.audit_plus.checkpoint",
        "action": "checkpoint",
    })
    return await context.async_request_model(
        prompt={
            "task": task,
            "selected_skills": plan["selected_skills"],
            "policy": effort_config,
        },
        model_key="verifier",
        output_schema={"decision": (str, "go / no-go", True)},
        output_format="json",
    )

Agently.skills_executor.register_effort_strategy(
    "audit_plus",
    audit_plus_strategy,
)

agent.set_settings("effort_presets", {
    "audit_plus": {"strategy": "audit_plus", "custom_budget": 7},
})

execution = await agent.async_run_skills_task(
    "Audit this release.",
    skills=["release-review"],
    mode="required",
    effort="audit_plus",
)
```

Skills runtime 内部模型调用使用符号阶段 key：`planner`、`research`、`reason`、
`executor`、`verifier`、`reflector` 和 `finalizer`。如果某个 key 没有在
`model_pool` 里映射，Agently 会沿用 agent 继承来的模型配置，而不会把这个符号 key
当成 provider model name 发出去。

通过 Agent 自动编排选中 Skills route 时，模型字段流会桥接到稳定路径，例如
`skills.model.fields.<field_path>`。

安装 Skill 不会自动执行 bundled scripts 或资源。标准 Skill 如果在正文、资源、
`compatibility` 或公开 `metadata` 里表达了 search、browse、HTTP、Workspace file、
Python、shell/script 或 MCP 需求，Skills Executor 会在 plan 里记录结构化
`capability_needs`。Skill 仍然不授予能力。执行前，Agently 会把这些需求和宿主
policy 对照；只有明确标记为 `allow` 的内置能力会被自动加载，`approval` 和 `off`
都会 fail closed 并返回诊断。

```python
agent.configure_skill_capabilities(
    auto_load={
        "web_search": "allow",
        "web_browse": "allow",
        "workspace_write": "allow",
        "script_run": "approval",
        "shell": "approval",
        "mcp": "approval",
    },
    workspace_root="./.agently/tasks/research",
    search={
        "backend": "auto",
        "refresh_ddgs": "allow",
    },
)
agent.configure_policy_approval(handler="input_timeout_fail")
```

面向搜索的 Skills，Agently 会装载由 `ddgs` Python package 支撑的框架 Search
能力。真实搜索前建议保持 `ddgs` 最新：
`python -m pip install --upgrade ddgs`。backend 策略不能被固定成某一个 provider；
默认使用 `backend="auto"`，也可以由宿主 policy 配置任何 ddgs 支持的 backend。
Search 会把 backend 层面的“无结果”视为成功空结果，并在选定 backend 没有解析到
可用结果时继续尝试配置或默认的 ddgs fallback backends。如果一个或多个 backend
失败后由 fallback backend 找到可用结果，Search 会返回
`status="partial_success"`、`success=True` 和 backend diagnostics，让任务继续
使用证据，同时让操作者看到哪些搜索源发生了降级。

Workspace 文件操作归 Workspace 边界所有。Agent 已绑定 Workspace 时，
SkillsExecutor 会优先通过 Workspace 文件边界暴露文件 actions，再退回
`agent.enable_workspace_file_actions(...)`。

`approval` 由框架全局 PolicyApproval handler 处理，不由 SkillsExecutor 私有 handler
处理。默认 handler 是 `input_timeout_fail`：交互式 CLI 会等待输入并在超时后失败，
非交互服务环境会立即失败。测试和可信本地 fixture 可以使用 `auto_approve`。
真实服务应根据包裹 TriggerFlow execution 的服务方法注册对应 handler，例如数据库 pending
approval 记录、HTTP callback、webhook resume、SSE/WebSocket 等待，或 save 后返回
interrupt id。需要 pending diagnostic 或 TriggerFlow `policy_approval` interrupt 时使用
`fail_closed`。

Skills Executor 不会把 `mcp`、`mcpServers`、`allow-scripts` 或
Agently-specific `allowed-actions` 等 Skill frontmatter 当成能力授权。公开
`compatibility` 和 `metadata` 可以作为发现 `capability_needs` 的证据，但加载仍由
宿主 policy 控制。
如果宿主希望在确定性读取 Skill 之外加入模型判断，可以开启
`skills.capability_discovery.model_assisted=True`；模型推断出的 needs 仍然只是
证据，必须经过同一套宿主 policy gate。

公开 Agent Skills 规范里的 `allowed-tools` 是实验字段。Agently 如果支持它，也只能
把它作为 already-mounted host tools 的限制或预批准提示；它不能挂载新 Actions、
创建 MCP client、开启 shell/file access，或合成缺失 backend。

## 验收样例

`examples/agent_auto_orchestration/19_remote_skills_weather_event_ops.py`
端到端验证了 4.1.3 的远程连接器路径：业务代码只通过 `agent.use_skills(...)`
声明公开远程 Skills；免费 weather MCP 服务通过 ActionRuntime 注册；模型生成 MCP
action calls 取得真实天气观测；Skills Executor 在命中后懒安装选中的 Skills，并用
`effort="normal"` 执行完整链路。

## 配置

Skill 的适用性来自 `SKILL.md`；Agently 的 `.agently/` 文件只是描述性的安装元数据。
多步 Skills 执行应组合 Agently 已有的 TriggerFlow、Action 和
ExecutionEnvironment 边界；人工审批或持久 wait/resume 应通过 TriggerFlow
`pause_for(...)` / `continue_with(...)`，或 Action / ExecutionEnvironment
审批策略表达，不应通过修改已关闭的 `SkillExecution` snapshot 来伪装恢复。

框架级 `skills.*` 配置仍可调整宿主行为，例如普通 prompt 是否披露可选 Skill
候选的完整 guidance。有 plugin defaults 时会先加载 plugin defaults，框架配置
是最终应用级默认值。两层配置都不能替代 `SKILL.md` 成为 Skill 能力定义。

本地 registry 相关配置应使用公开 Skills Executor 配置 helper：

```python
Agently.skills_executor.configure(
    registry_root="./.agently/skills-dev",
    allowed_trust_levels=["local"],
)
```

## API Summary

- `Agently.skills_executor.install_skills(...)`
- `Agently.skills_executor.install_skills_pack(...)`
- `Agently.skills_executor.configure(...)`
- `Agently.skills_executor.inspect_skills(...)`
- `agent.use_skills(...)`
- `agent.use_skills_packs(...)`
- `agent.resolve_skills_plan(...)`
- `agent.run_skills_task(...)`

`SkillContract` 描述已安装的标准 Skill、Agently 安装元数据、decision card、
资源索引和 checksums，不包含框架自创的 stage 声明。
