---
title: Agently 4.1.4.8 发布说明
description: Fluent AgentExecution typing、review 与 artifact 交付、Execution plugins、execution-scoped Skills、Action runtime 改进及发布证据。
keywords: Agently, 4.1.4.8, typing, IDE, AgentExecution, plugin, Action, Skill, Ollama
---

# Agently 4.1.4.8 发布说明

当前为未发布候选。以下说明已实现的用法，不代表所有发布验收门槛已通过。

Agently 4.1.4.8 聚焦执行组合与开发体验。单次 Agent 代码在 IDE 中更容易阅读：
fluent chain 会持续返回同一个 `AgentExecution`，其 Actions、Skills、interaction
handler、review 与 artifact 均保持本轮局部。支持 `always` 参数的
配置方法只有在显式使用 `always=True` 时才写入 Agent 默认配置。

本版要求 Python 3.10 或更高版本以及 `agently-stage >=0.3.8,<0.4.0`；推荐
`agently-devtools >=0.1.11,<0.2.0`，并配套对齐 Agently 4.1.4.8 的
Agently-Skills V2 catalog。

## 推荐用法

以下使用显式配置的 OpenAI-compatible 服务和演示账户数据；真实项目应把 Action 实现接到自己的账户系统。

```python
import os
from agently import Agently

Agently.set_settings("plugins.ModelRequester.OpenAICompatible", {
    "base_url": os.environ["MODEL_BASE_URL"],
    "auth": os.environ["MODEL_API_KEY"],
    "model": os.environ["MODEL_NAME"],
})
agent = Agently.create_agent("renewal-review")
agent.use_task_workspace("./workspace")


def load_account(account_id: str) -> dict[str, str]:
    """演示数据适配器；不代表已查询真实账户系统。"""
    return {"account_id": account_id, "renewal_risk": "medium"}


execution = (
    agent
    .input({"account_id": "acct-42"})
    .info("只使用已观测账户事实，并标记未知项。")
    .use_action(load_account)
    .output({"recommendation": (str, "有事实依据的下一步", True)})
    .review()
    .artifact("reports/acct-42.json")
)

result = execution.get_result()
print(result.get_data())
print(execution.artifact_results)
```

Pylance 与 Pyright 现在能在 Action/Skill 链之后继续推断 `AgentExecution`。
`always=True` 仍是明确的 Agent-default 形式：

```python
agent.use_actions(load_account, always=True)

one_run = agent.create_execution("plan").input("起草上线计划。")
```

Skills 采用同样的返回类型规则；Skill 注册与 exact-revision 绑定参见
[release-pinned Skill 示例](../../../examples/release_pinned_usage/03_skill_library_agent_binding.py)。

IDE 会为 `create_execution`、`effort`、`strategy`、`planning_protocol` 和 Action
`concurrency_mode` 显示内置候选；公开合同允许扩展的位置仍接受插件 Execution 名或
替代 orchestrator strategy 名。

## 核心变动

| 领域 | 变动内容 | 推荐用法 | 兼容性 / 风险 | 证据 |
|---|---|---|---|---|
| Typing 与 IDE | Action/Skill fluent 方法区分本轮 `AgentExecution` 和 `always=True` Agent 默认；公开 execution 方法补充可读 docstring 与有限选项。 | 把一次性的 `.input(...).info(...).use_action(...)` 或 `.use_skills(...)` 保持在一条链上。 | 增量提高 typing 精度；不支持的有限值现在会被静态检查拒绝。 | 全仓 Pyright、公开 Any allowlist、负向 typing fixture、安装 wheel 的外部 smoke。 |
| Result streaming | 重新打开 instant stream 时重放通过校验的 retry attempt；AgentExecution 保留 rejected/accepted attempt 顺序。 | provisional UI 使用 `attempt_index`，最终 parsed data 仍是权威。 | 兼容性修复。 | `examples/release_pinned_usage/06_validate_retry_accepted_stream.py` 与确定性测试。 |
| Model 选择 | 显式未知 model alias 在 provider dispatch 前失败；`resolve_model_profile` 提供不含 secret 的预检视图。 | 启动业务工作前校验配置的 model key。 | 配置非空 `model_pool` 时对拼写错误 fail-closed。 | Model configuration tests 与 `examples/model_configures/typed_settings_and_model_profiles.py`。 |
| Action Runtime | `programmatic` planning 可执行一个有界、只读的 Action micro-DAG；Action 可声明 `parallel` / `exclusive`。 | 默认继续使用 `structured_plan`；仅在 Action 合格且有隔离 code provider 时显式启用。 | 显式 opt-in、受 policy 约束，不承诺普遍降低成本或延迟。 | Action runtime suites 与 `examples/action_runtime/4_4_programmatic_vs_structured_deepseek.py`。 |
| Action 交付与 debug | 终态 Action response 复用当前 execution result；并发 console stream 按 first-delta FIFO 展示且不串行化执行。 | `debug=True` 用于可读展示，EventCenter/DevTools 保存完整事实。 | 仅展示层改变；事件和执行顺序仍由运行时负责。 | Pinned examples 04、05 与 console/action tests。 |
| Skills | Agent 默认与 execution-local 声明冻结一个 exact-revision scope；脚本仍是 `selected_resources` 中的惰性资源，读取不创建 Action 或授权。 | Agent 默认使用 `always=True`，本轮增量使用 execution 方法；显式挂载执行能力。 | Scope fail-closed；无隐式 script actionization。开发期非空脚本候选示例已撤回；兼容 facade 保留已发布的 `action_candidates: []` 字段。 | Pinned examples 03、07，Skills tests、Agently-Skills V2 guidance。 |
| Agent 交付策略 | `interact`、`review(rules=..., on_fail=...)` 与经 TaskWorkspace 校验的 `artifact` 成为稳定公开方法。 | 把 handler 绑定在拥有结果与 artifact 的 execution 上。 | 增量能力；blocking review 可以阻止终态成功。 | Examples 25、26 与 AgentExecution handler/artifact tests。 |
| Execution 插件 | `create_execution(name)` 直接返回注册的执行实例；内置 `auto`、`request`、`long_task`、`plan`、`long_content`。 | 按需显式选择生产方；`.goal(..., turn_on_long_task=False)` 只声明语义目标。 | 替换未发布的 Pattern；已发布 Orchestrator/AgentTask 入口保留为兼容适配。 | Examples 26–28 及插件身份、目标补全、最终策略与 typing tests。 |
| MCP | Playwright MCP examples 覆盖本地生命周期与模型驱动浏览器使用。 | 让 ExecutionResource 管理 MCP session 并确定性关闭。 | 依赖外部 runtime/browser。 | `examples/action_runtime/2_3_mcp_playwright_e2e_local.py` 与 `2_4_mcp_playwright_agent_qwen.py`。 |
| 长文与续写 | `long_content` 负责结构长文生产；`LongContent` 字段独立生成后填回结构；`auto_continue` 只接续未完成的请求。 | 长字段使用 `(LongContent, "写作要求")`；按需启用 `.auto_continue()`。 | 兼容 `"long_content"` 类型表达和旧 `.ensure_long_output()`；不强制触发续写。 | `examples/basic/auto_continue.py`、`examples/agent_auto_orchestration/29_field_long_content_ollama.py`、续写/输出控制测试。 |
| 执行控制 | 安全边界暂停/恢复、保存/加载与同对象 revision 返工；旧 reader 保留原结果。 | 使用 execution 的 `pause/resume/save/load/rework` 及异步对应方法，先检查 `control_capabilities`。 | 不承诺恢复活跃 provider、活动子执行或完整嵌套预算。 | 统一执行控制、生命周期/返工/快照及安装后 typing 测试。 |
| 音频 | 独立 `AudioModelRequest` 提供 TTS/STT，Agent 显式挂载；四种组合流区分连续 PCM、独立音频段、转录块和文字句末。 | `Agently.create_audio_request(...)` → `agent.use_audio(audio)`；流使用 `async with`。 | 不复用文本 Prompt；不隐式录音/播放；内置驱动尚无原生实时 STT 输入。 | [音频用法](../models/audio.md)、`examples/audio/tts_stt_roundtrip.py`、`examples/audio/continuous_audio.py`、音频测试。 |
| Shell（未完成范围） | 原生进程核心与 Cmd 反向委托已实现；三档环境、四档审批及通用 Agent 新入口仍未完成。 | 当前旧 Cmd/enable_shell 仍保留 argv 语义，不把它当作完整 Bash/PowerShell 脚本接口。 | **待完成，不是已支持能力**；CrossOver 环境探针不替代 Windows 原生隔离验收。 | Shell/Cmd 生命周期测试；完整功能验收仍开放。 |

长文声明与续写配置彼此独立，例如复用上面的已配置 Agent：

```python
from agently import LongContent

execution = agent.input("写一份按章节组织的操作手册。").output({
    "body": (LongContent, "按任务要求展开正文；不要凭空增加业务约束。"),
}).auto_continue()
```

内置 SQLite RecordStore 和向量存储现在会在每次操作退出时关闭连接，保留原有
提交、回滚及异常传播。这修复了连接泄漏，不改变公开调用、只读策略或惰性创建行为。

内置 RecordStore 的上下文快照现在按实际可见范围校验版本，其他任务在范围外写入不会
使当前 Reader 误失效；可见数据变化仍需要刷新。页面和精确读回使用同一只读事务的版本，
ContextSource 不读取范围外记录，公共 RecordStore 读取不因此新增权限规则。
自定义 provider/读取适配仍走原路径。版本检查仍需扫描元数据，不承诺与记录规模无关的开销。

任务修复的结构化要求和证据标识现在会保留到下一轮规划，包含经摘要保存/恢复的路径；
历史快照中已经丢失的要求不会被凭空重建。

## 本版补齐的 Examples

- `25_agent_execution_delivery_review_ollama.py`：真实本地 Qwen 生成、模型 review、
  blocking handler review 与物理 artifact readback。
- `26_plan_execution_interaction_ollama.py`：一次 connected clarification exchange、
  host-validated plan 与 artifact 交付。
- `27_long_content_execution_artifact_ollama.py`：section planning/writing、host 顺序组装、
  artifact 校验与 advisory review。
- `28_missing_goal_preparation_ollama.py`：为显式选定的长任务按需推导缺失目标。
- Release-pinned examples 06、07：无模型依赖地锁定 accepted retry stream 与
  execution-scoped Skill composition。

Ollama examples 默认使用 `qwen`；可通过
`AGENT_EXECUTION_OLLAMA_MODEL` 或 `OLLAMA_DEFAULT_MODEL` 切换其他本地 Qwen。

早期重构检查点（不是当前最终验收结论）：26/27 实跑完成了框架交付，但语义检查分别发现虚构参与人数门槛、
扩大部署限制；28 完成目标补全后在后续生产阶段超时。这些问题保留为 Prompt 审查项，
不算语义发布验收通过。统一控制现已覆盖外层安全暂停/恢复/快照、取消/关闭、
补充信息，以及同对象 revision 返工、历史 reader 和累计预算。活跃子执行或 provider
checkpoint、离线澄清和嵌套父预算恢复仍不支持。用法见[统一执行控制](../start/auto-orchestration.md#统一执行控制)。
这一检查点不代表发布验收完成。

## 兼容性与发布门禁

- Package version：`4.1.4.8`。
- Release manifest：`compatibility/releases/4.1.4.8.json`。
- 必需 runtime：`agently-stage >=0.3.8,<0.4.0`（已核实 PyPI 0.3.8）。
- 可选 observation companion：`agently-devtools >=0.1.11,<0.2.0`，协议为
  `agently-devtools.observation-runtime.v1`。
- Agently-Skills：V2 catalog，aligned framework version `4.1.4.8`。

根据仓库发布规则，本地 Ollama/Qwen 运行属于补充证据。最终推荐发布前，release PR
还必须记录线上模型 Foundation 与 pinned-example 检查，或记录维护者明确 waiver 和
残余风险。构建或测试本候选版本不会自动消耗线上 API quota。

发布后安装：

```bash
pip install -U "agently==4.1.4.8"
```
