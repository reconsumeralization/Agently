---
title: Agently 4.1.4.8 发布说明
description: Fluent AgentExecution typing、review 与 artifact 交付、beta Patterns、execution-scoped Skills、Action runtime 改进及发布证据。
keywords: Agently, 4.1.4.8, typing, IDE, AgentExecution, Pattern, Action, Skill, Ollama
---

# Agently 4.1.4.8 发布说明

Agently 4.1.4.8 聚焦执行组合与开发体验。单次 Agent 代码在 IDE 中更容易阅读：
fluent chain 会持续返回同一个 `AgentExecution`，其 Actions、Skills、interaction
handler、review、verification 与 artifact 均保持本轮局部。支持 `always` 参数的
配置方法只有在显式使用 `always=True` 时才写入 Agent 默认配置。

本版要求 Python 3.10 或更高版本以及 `agently-stage >=0.3.8,<0.4.0`；推荐
`agently-devtools >=0.1.11,<0.2.0`，并配套对齐 Agently 4.1.4.8 的
Agently-Skills V2 catalog。

## 推荐用法

以下使用本地 Ollama 的演示账户数据；真实项目应把 Action 实现接到自己的账户系统。

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
    "base_url": "http://127.0.0.1:11434/v1",
    "api_key": "ollama-local",
    "model": "qwen3.5:9b",
    "model_type": "chat",
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

one_run = agent.input("起草上线计划。").pattern("plan")
```

Skills 采用同样的返回类型规则；Skill 注册与 exact-revision 绑定参见
[release-pinned Skill 示例](../../../examples/release_pinned_usage/03_skill_library_agent_binding.py)。

IDE 会为 `pattern`、`effort`、`strategy`、`planning_protocol` 和 Action
`concurrency_mode` 显示内置候选；公开合同允许扩展的位置仍接受插件 Pattern 名或
替代 orchestrator strategy 名。

## 核心变动

| 领域 | 变动内容 | 推荐用法 | 兼容性 / 风险 | 证据 |
|---|---|---|---|---|
| Typing 与 IDE | Action/Skill fluent 方法区分本轮 `AgentExecution` 和 `always=True` Agent 默认；公开 execution 方法补充可读 docstring 与有限选项。 | 把一次性的 `.input(...).info(...).use_action(...)` 或 `.use_skills(...)` 保持在一条链上。 | 增量提高 typing 精度；不支持的有限值现在会被静态检查拒绝。 | 全仓 Pyright、公开 Any allowlist、负向 typing fixture、安装 wheel 的外部 smoke。 |
| Result streaming | 重新打开 instant stream 时重放通过校验的 retry attempt；AgentExecution 保留 rejected/accepted attempt 顺序。 | provisional UI 使用 `attempt_index`，最终 parsed data 仍是权威。 | 兼容性修复。 | `examples/release_pinned_usage/06_validate_retry_accepted_stream.py` 与确定性测试。 |
| Model 选择 | 显式未知 model alias 在 provider dispatch 前失败；`resolve_model_profile` 提供不含 secret 的预检视图。 | 启动业务工作前校验配置的 model key。 | 配置非空 `model_pool` 时对拼写错误 fail-closed。 | Model configuration tests 与 `examples/model_configures/typed_settings_and_model_profiles.py`。 |
| Action Runtime | `programmatic` planning 可执行一个有界、只读的 Action micro-DAG；Action 可声明 `parallel` / `exclusive`。 | 默认继续使用 `structured_plan`；仅在 Action 合格且有隔离 code provider 时显式启用。 | 显式 opt-in、受 policy 约束，不承诺普遍降低成本或延迟。 | Action runtime suites 与 `examples/action_runtime/4_4_programmatic_vs_structured_deepseek.py`。 |
| Action 交付与 debug | 终态 Action response 复用当前 execution result；并发 console stream 按 first-delta FIFO 展示且不串行化执行。 | `debug=True` 用于可读展示，EventCenter/DevTools 保存完整事实。 | 仅展示层改变；事件和执行顺序仍由运行时负责。 | Pinned examples 04、05 与 console/action tests。 |
| Skills | Agent 默认与 execution-local 声明冻结一个 exact-revision scope；script discovery 只返回 inert candidate，等待 host 显式授权。 | Agent 默认使用 `always=True`，本轮增量使用 execution 方法。 | Scope fail-closed；无隐式 script actionization。 | Pinned examples 03、07、Skills tests、Agently-Skills V2 guidance。 |
| Agent 交付策略 | `interact`、advisory `review`、required `verify` 与经 TaskWorkspace 校验的 `artifact` 成为稳定公开方法。 | 把 handler 绑定在拥有结果与 artifact 的 execution 上。 | 增量能力；verification 按设计可以使终态运行失败。 | Examples 25、26 与 AgentExecution handler/artifact tests。 |
| Patterns | 通过 `.pattern(...)` 提供内置 `plan` 与 `long_content` whole-request Pattern。 | 每次 execution 显式选择，最终业务值仍由 AgentExecution 交付。 | Beta；不兼容的 delivery contract 会在 dispatch 前失败。 | Examples 26、27 与 Pattern isolation/contract tests。 |
| MCP | Playwright MCP examples 覆盖本地生命周期与模型驱动浏览器使用。 | 让 ExecutionResource 管理 MCP session 并确定性关闭。 | 依赖外部 runtime/browser。 | `examples/action_runtime/2_3_mcp_playwright_e2e_local.py` 与 `2_4_mcp_playwright_agent_qwen.py`。 |

## 本版补齐的 Examples

- `25_agent_execution_delivery_review_ollama.py`：真实本地 Qwen 生成、模型 review、
  required handler verification 与物理 artifact readback。
- `26_plan_pattern_interaction_ollama.py`：一次 connected clarification exchange、
  host-validated plan 与 artifact 交付。
- `27_long_content_pattern_artifact_ollama.py`：section planning/writing、host 顺序组装、
  artifact 校验与 advisory review。
- Release-pinned examples 06、07：无模型依赖地锁定 accepted retry stream 与
  execution-scoped Skill composition。

Ollama examples 默认使用 `qwen3.5:9b`；可通过
`AGENT_PATTERN_OLLAMA_MODEL` 或 `OLLAMA_DEFAULT_MODEL` 切换其他本地 Qwen。

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
