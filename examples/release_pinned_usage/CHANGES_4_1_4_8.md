# 4.1.4.8：示例变更与兼容导航 / Example change guide

本表比较已发布的 4.1.4.7 与 4.1.4.8 候选，不把开发中间方案当作已发布 API。
它补充示例的变更说明，不更改锁定脚本，也不把确定性协议探针当作模型质量证明。
双语完整说明：[中文](../../docs/cn/development/release-notes-4.1.4.8.md)、
[English](../../docs/en/development/release-notes-4.1.4.8.md)。

This guide compares released 4.1.4.7 with the 4.1.4.8 candidate, not with an
intermediate development design. Protocol probes protect compatibility; real
model examples demonstrate scenarios, not guarantees for every model.

## 核心变动 / Core changes

| 领域 / Area | 变更与推荐用法 / Change and usage | 兼容及边界 / Compatibility and risk | 示例与说明 / Examples and docs |
|---|---|---|---|
| Execution | `create_execution(name)` 选择生产插件；不指定时仍为 auto。Select an explicit producer only when needed. | 已发布 AgentTask/Orchestrator 入口保留；未发布 Pattern 被替换。Released adapters remain; no Pattern migration is required for 4.1.4.7 users. | [22](../agent_auto_orchestration/22_unified_agent_execution_result.py)、[23](../agent_auto_orchestration/23_agent_execution_auto_dispatch.py)、[指南 / guide](../../docs/en/start/auto-orchestration.md) |
| Fluent typing / scope | 本轮 Action/Skill 配置保持同一 execution；`always=True` 声明 Agent 默认。Keep one-run configuration on one chain. | 新请求不继承上一轮局部授权；IDE 提示更精确。No implicit authority inheritance. | [01](01_agent_execution_result_lifecycle.py)、[03](03_skill_library_agent_binding.py)、[07](07_skill_execution_scope.py) |
| Plan / goal | 显式 `plan` 支持必要的已连接澄清；缺目标补全只在需要它的生产模式中发生。Connected clarification; conditional goal preparation. | 普通请求不自动加规划节点；`goal(..., turn_on_long_task=False)` 只声明目标。No universal planning preflight. | [26](../agent_auto_orchestration/26_plan_execution_interaction_ollama.py)、[28](../agent_auto_orchestration/28_missing_goal_preparation_ollama.py) |
| validate / review / artifact | `validate` 校验最终结果；artifact 完整读回；`review(rules=..., on_fail="warn"/"block")` 检查终态质量。Final policies have separate responsibilities. | review 默认不自动开启、不自动返工；block 不回滚已写文件。No public `verify()` added. | [25](../agent_auto_orchestration/25_agent_execution_delivery_review_ollama.py)、[指南 / guide](../../docs/en/start/auto-orchestration.md) |
| Long content | 显式长文模式按完整计划写当前章，正文与章级摘要分离，Host 按序组装。Chapter-level summaries, not one summary per heading. | 普通 `str` 不强制长文；显式字段可用 `(LongContent, "要求")` 或兼容字符串表达。Final fields remain strings. | [27](../agent_auto_orchestration/27_long_content_execution_artifact_ollama.py)、[字段 / fields](../agent_auto_orchestration/29_field_long_content_ollama.py)、[输出 / output](../../docs/en/requests/output-control.md) |
| Continuation | `.auto_continue()` 条件接续未完成的纯文本或结构内字符串。Conditional delivery, independent of readers. | `.ensure_long_output()` 保留；默认关闭，正常完成不增加续写请求，不等于业务长文生产。No forced continuation. | [auto_continue](../basic/auto_continue.py)、[输出 / output](../../docs/en/requests/output-control.md) |
| Execution controls | `pause/resume/save/load/rework` 与历史 reader。Use `control_capabilities` to inspect supported boundaries. | resume 不等于 rework；仅安全边界快照，副作用重放需许可。No active child/provider recovery. | [controls](../agent_auto_orchestration/29_execution_controls_ollama.py)、[指南 / guide](../../docs/en/start/auto-orchestration.md) |
| TTS / STT | 独立 AudioModelRequest，`agent.use_audio(audio)` 显式挂载；连续流与 auto-break 输出块分开。Explicit audio dependency, separate output modes. | 不改变文本 ModelRequest/Prompt；未挂载报错；没有隐式麦克风/播放。Native realtime STT input is not implemented by built-in drivers. | [roundtrip](../audio/tts_stt_roundtrip.py)、[streams](../audio/continuous_audio.py)、[audio](../../docs/en/models/audio.md) |
| Shell / Cmd | 无旧参数的 `enable_shell()` 进入通用 Shell，默认 `offline` / `all`；Host 选择环境和审批。Model supplies command/workdir only. | 显式 `commands`/`sandbox` 保留旧 argv 模式并提示迁移；Cmd 反向委托，4.2 才计划清理。Missing isolation never falls back to host. | [general Shell](../action_runtime/3_8_general_shell_model.py)、[Cmd](../builtin_actions/04_cmd_async_lifecycle_local.py)、[Shell guide](../../docs/en/actions/shell.md) |
| Action / PTC | `programmatic` 为显式只读、有界隔离执行；普通 `structured_plan` 不变。Host checks actual offered scope. | 不因新协议扩大权限；不承诺更快或更便宜。No implicit PTC selection. | [comparison](../action_runtime/4_4_programmatic_vs_structured_deepseek.py)、[Action docs](../../docs/en/actions/action-runtime.md) |
| Flat / B4 | 依赖前次 Action 的参数等观察就绪再生成；已读资料不丢失，普通中间成功观察进入下一步。Wait for new evidence before dependent requests. | 保留最终验收和累计 required 门槛；不引入新 loop，也未采用无稳定收益的合并候选。Existing final checks remain. | [dependency](../agent_task/action_result_dependency.py)、[指南 / guide](../../docs/en/start/auto-orchestration.md) |
| Skills / Context | 根指引传到资源选择；已绑定来源可渐进读取。New user requests get fresh executions. | 保留已发布 `action_candidates: []`；撤回开发期非空脚本候选例。Reading Skills grants no execution permission. | [07](07_skill_execution_scope.py)、[conditional read](../skills_executor/11_conditional_resource_read.py)、[Skills docs](../../docs/en/development/skills-executor.md) |
| Request / models | accepted retry stream 可重读；配置了非空 model_pool 时未知 alias 报错；增加无密钥值预检。Original output descriptions remain available. | 普通请求/生成器用法保留；错误 alias 不再静默继承。Instant output is provisional. | [06](06_validate_retry_accepted_stream.py)、[model profiles](../model_configures/typed_settings_and_model_profiles.py)、[RootModel](../basic/pydantic_root_output.py) |
| Debug / MCP / storage | FIFO 只改变 console 展示；MCP 生命周期、SQLite close/可见范围版本修复。Keep event and storage owners unchanged. | 不串行化执行；TaskWorkspace 管文件，RecordStore 管记录。No new storage facade. | [04](04_debug_console_profiles.py)、[05](05_action_response_delivery.py)、[MCP](../action_runtime/2_3_mcp_playwright_e2e_local.py)、[22](../agent_auto_orchestration/22_unified_agent_execution_result.py) |
| Deferred / 后移 | 更广 Skills/Execution 组合、long_task/TaskBoard Prompt 与 review 深度融合放在 4.1.4.9 规划；已公告旧入口清理面向 4.2。 | 不属于本版已实现能力；Python 3.10 本版仍支持。Not a promise of active-process restoration or a new orchestration model. | [版本边界 / release boundaries](../../docs/en/development/release-notes-4.1.4.8.md) |

## 新旧调用 / Before and after

下面复用项目已配置的 `agent`。独立创建示例，避免在同一个已启动 execution 上重复配置。
These snippets reuse a configured Agent and create independent drafts.

```python
# 4.1.4.7 spelling: still supported in 4.1.4.8.
legacy = agent.input("Write the requested report.").ensure_long_output()
# 4.1.4.8 recommended spelling; same conditional policy, not a producer.
current = agent.input("Write the requested report.").auto_continue()

# Released restricted argv path: kept when these arguments are explicit.
agent.enable_shell(root=".", commands=["pwd", "ls"])
# General Shell: isolated, every operation needs the configured approval handler.
# Configure this on a separate Agent instead of mounting both examples together.
# agent.enable_shell(root=".", environment="offline", approval="all")
```

未使用修订能力的普通 Prompt/结果读取、Session、TriggerFlow/TaskDAG、向量/embedding
和插件管理不需要整体迁移。相关修复不代表这些主功能被重设计。
Existing ordinary requests, Session, TriggerFlow/TaskDAG, embedding/vector APIs
and plugin management do not require a wholesale migration.

Windows 证据仅含 CrossOver；原生 Windows 用户请实测并提交最小复现 issue。
模型生成质量仍依赖模型和输入，warn review 可以返回不通过评价及原结果；不把它写成
严格质量保证。Native Windows isolation is not established by CrossOver tests.
