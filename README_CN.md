<img width="640" alt="image" src="https://github.com/user-attachments/assets/c645d031-c8b0-4dba-a515-9d7a4b0a6881" />

# Agently 4 🚀

> **让生产级 AI 应用开发更快、更稳、更可维护**

[English Introduction](https://github.com/AgentEra/Agently/blob/main/README.md) | [中文介绍](https://github.com/AgentEra/Agently/blob/main/README_CN.md)

[![license](https://img.shields.io/badge/许可证-Apache%202.0-blue.svg)](https://github.com/AgentEra/Agently/blob/main/LICENSE)
[![PyPI version](https://img.shields.io/pypi/v/agently.svg)](https://pypi.org/project/agently/)
[![Downloads](https://img.shields.io/pypi/dm/agently.svg)](https://pypistats.org/packages/agently)
[![GitHub Stars](https://img.shields.io/github/stars/AgentEra/Agently.svg?style=social)](https://github.com/AgentEra/Agently/stargazers)
[![Twitter Follow](https://img.shields.io/twitter/follow/AgentlyTech?style=social)](https://x.com/AgentlyTech)
<a href="https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf">
<img alt="WeChat" src="https://img.shields.io/badge/微信交流群-加入我们-brightgreen?logo=wechat&style=flat-square">
</a>

<p align="center">
  <a href="https://github.com/AgentEra/Agently/discussions"><img src="https://img.shields.io/badge/💬_社区讨论-分享想法-blueviolet?style=for-the-badge"></a>
  <a href="https://agently.cn"><img src="https://img.shields.io/badge/🌐_访问官网-获取文档-brightgreen?style=for-the-badge"></a>
  <a href="https://github.com/AgentEra/Agently/issues"><img src="https://img.shields.io/badge/🐛_报告问题-帮助改进-red?style=for-the-badge"></a>
</p>

---

<p align="center">
  <b>🔥 <a href="https://agently.cn/docs">最新文档与更新</a> | 🚀 <a href="#快速开始">5 分钟快速开始</a> | 💡 <a href="#-核心特性">核心特性</a></b>
</p>

---

## 📚 核心资源

- **官方文档（中文）**: https://agently.cn/docs
- **官方文档（英文）**: https://agently.tech/docs
- **智能体系统 Playbook（中文）**: https://agently.cn/docs/agent-systems/overview.html
- **智能体系统 Playbook（英文）**: https://agently.tech/docs/en/agent-systems/overview.html
- **Coding Agent 指南（中文）**: https://agently.cn/docs/agent-docs.html
- **Coding Agent 指南（英文）**: https://agently.tech/docs/en/agent-docs.html
- **Agent 文档包（离线版）**: https://agently.cn/docs/agent_docs.zip

## 🤔 为什么需要 Agently？

很多 GenAI POC 难以落地投产，问题往往不在模型“聪明与否”，而在**工程可控性不足**：

| 常见痛点 | Agently 的解决方案 |
|:--|:--|
| 输出结构漂移、JSON 解析失败 | **契约式输出控制**：`output()` + `ensure_keys` 保障关键字段稳定出现 |
| 工作流越来越复杂、难维护 | **TriggerFlow 编排**：`to` / `if` / `match` / `batch` / `for_each` 让逻辑可读可测 |
| 多轮对话上下文不稳定 | **Session & Memo**：记忆、摘要、持久化与裁剪策略 |
| 工具调用不可追踪 | **工具日志**：`extra.tool_logs` 可审计、可复盘 |
| 切换/升级模型成本高 | **统一模型配置**：`OpenAICompatible` 适配多家云端/本地模型 |

**Agently 的核心价值是把 LLM 的不确定性转化为“稳定、可测试、可维护”的工程系统。**

## ✨ 核心特性

### 1) 📝 契约式输出控制
用 `output()` 明确结构，用 `ensure_keys` 保障关键字段稳定出现，减少集成失败与返工。

```python
result = (
    agent
    .input("分析用户反馈")
    .output({
        "情感倾向": (str, "积极/中立/消极"),
        "关键问题": [(str, "总结的问题点")],
        "紧急程度": (int, "1-5分")
    })
    .start(ensure_keys=["情感倾向", "关键问题[*]"])
)
```

### 2) ⚡ 结构化流式（Instant）
结构化字段边生成边可用，适合实时 UI 与动作触发。

```python
response = (
    agent
    .input("解释递归，并给出 2 个提示")
    .output({"definition": (str, "一句话定义"), "tips": [(str, "提示")]})
    .get_response()
)

for msg in response.get_generator(type="instant"):
    if msg.path == "definition" and msg.delta:
        ui.update_definition(msg.delta)
    if msg.wildcard_path == "tips[*]" and msg.delta:
        ui.add_tip(msg.delta)
```

### 3) 🧩 TriggerFlow 工作流编排
支持 `to`、`if/elif`、`match/case`、`batch`、`for_each`，逻辑清晰可读、可测、可扩展。

```python
(
    flow.to(接收请求)
    .if_condition(lambda d: d.value["type"] == "查询")
    .to(执行查询)
    .elif_condition(lambda d: d.value["type"] == "订购")
    .to(验证库存)
    .to(创建订单)
    .end_condition()
)
```

### 4) 🧠 Session & Memo 多轮对话管理
支持 Quick / Lite / Memo 模式，提供记忆、摘要、持久化与裁剪策略。

```python
from agently import Agently
from agently.core import Session

agent = Agently.create_agent()
session = Session(agent=agent).configure(
    mode="memo",
    limit={"chars": 6000, "messages": 12},
    every_n_turns=2,
)
agent.attach_session(session)
```

### 5) 🔧 工具调用与日志
工具注册与调用记录在 `extra.tool_logs`，便于排障与审计。

```python
@agent.tool_func
def add(a: int, b: int) -> int:
    return a + b

response = agent.input("12+34=?").use_tool(add).get_response()
full = response.get_data(type="all")
print(full["extra"]["tool_logs"])
```

### 6) 🌐 统一模型配置（OpenAICompatible）
一套配置适配多家云端与本地模型，降低供应商锁定成本。

```python
from agently import Agently

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "auth": "DEEPSEEK_API_KEY",
    },
)
```

## 🚀 快速开始

### 安装

```bash
pip install -U agently
```

*要求：Python ≥ 3.10，建议使用 Agently ≥ 4.0.7.2*

### 5 分钟上手体验

**1. 基础使用：结构化输出**

```python
from agently import Agently

agent = Agently.create_agent()

result = (
    agent.input("用一句话介绍 Python，并列出 2 个优点")
    .output({
        "introduction": (str, "一句话介绍"),
        "advantages": [(str, "优点描述")]
    })
    .start(ensure_keys=["introduction", "advantages[*]"])
)

print(result)
```

**2. 进阶示例：工作流路由**

```python
from agently import TriggerFlow, TriggerFlowEventData

flow = TriggerFlow()

@flow.chunk
def classify_intent(data: TriggerFlowEventData):
    user_input = data.value
    if "价格" in user_input:
        return "price_query"
    if "功能" in user_input:
        return "feature_query"
    if "购买" in user_input:
        return "purchase"
    return "other"

@flow.chunk
def handle_price(_: TriggerFlowEventData):
    return {"response": "具体价格根据配置不同..."}

@flow.chunk
def handle_feature(_: TriggerFlowEventData):
    return {"response": "我们的产品支持..."}

(
    flow.to(classify_intent)
    .match()
    .case("price_query")
    .to(handle_price)
    .case("feature_query")
    .to(handle_feature)
    .case_else()
    .to(lambda d: {"response": "请问您想了解什么？"})
    .end_match()
    .end()
)

print(flow.start("这个产品多少钱？"))
```

## ✅ 你的应用准备好上线了吗？—— 生产级自检指南

基于 Agently 将大量项目送上线的经验，我们总结出这份 **「生产就绪检查清单」**。在发布前，逐项确认，能有效规避常见风险。

| 关注维度 | 检查项 | 推荐做法 |
| :--- | :--- | :--- |
| **📝 输出稳定性** | 关键数据接口是否稳定？ | 使用 `output()` 定义结构，并用 `ensure_keys` 锁定必返字段。 |
| **⚡ 实时体验** | UI 是否需要边生成边响应？ | 消费 `type="instant"` 的结构化流式事件，实现实时更新。 |
| **🔍 可观测性** | 工具调用能否审计与复盘？ | 查阅 `extra.tool_logs`，确保每次调用参数与结果可追溯。 |
| **🧩 流程健壮性** | 复杂工作流是否经过充分测试？ | 对 TriggerFlow 的每个分支、并发上限进行单元测试，验证预期输出。 |
| **🧠 记忆与上下文** | 多轮对话体验是否一致可控？ | 明确 Session/Memo 的摘要、裁剪与持久化策略。 |
| **📄 提示词管理** | 业务逻辑变更能否快速迭代？ | 将 Prompt 配置化、版本化管理，实现变更可追踪。 |
| **🌐 模型策略** | 能否灵活切换或降级模型？ | 通过 `OpenAICompatible` 集中配置，支持一键切换供应商。 |
| **🚀 性能与扩展** | 服务能否承受高并发？ | 在真实 Web 服务场景下，验证全链路异步性能。 |
| **🧪 质量保障** | 回归测试是否完备？ | 为每个核心场景编写固定输入与期望输出的测试用例。 |


## 📈 谁在用 Agently 解决真实问题？

> "Agently 帮助我们将评标细则转为可执行流程，模型评分关键项准确率稳定在 75%+，评标效率显著提升。" — 某能源央企项目负责人

> "Agently 让问数系统形成从澄清到查询到呈现的闭环，业务问题首次回复准确率达 90%+，上线后稳定运行。" — 某大型能源集团数据负责人

> "Agently 的工作流编排与会话能力，让教学助手在课程管理与答疑场景快速落地，并保持持续迭代。" — 某高校教学助手项目负责人

**你的项目也可以加入这个列表。**  
📢 [来 GitHub Discussions 分享你的案例 →](https://github.com/AgentEra/Agently/discussions/categories/show-and-tell)
## ❓ 常见问题

**Q：Agently 和 LangChain、LlamaIndex 等框架的主要区别是什么？**  
**A：** 定位不同。Agently **专为生产环境设计**，核心是解决“上线”后的工程问题：通过 **契约式输出** 保障接口稳定，通过 **声明式编排（TriggerFlow）** 实现复杂逻辑的可读可测，通过 **全链路日志（如 `tool_logs`）** 满足可观测性与审计需求。它更适合对交付稳定性、可维护性有较高要求的团队。

**Q：具体支持哪些模型？切换成本高吗？**  
**A：** 通过标准的 `OpenAICompatible` 接口，可无缝接入 OpenAI、Claude、DeepSeek、通义千问等几乎所有主流云端模型，以及本地部署的 Llama、Qwen 等开源模型。**一套业务代码，无需修改即可切换模型**，大幅降低供应商锁定风险和迁移成本。

**Q：框架的学习曲线如何？从哪里开始最好？**  
**A：** 基础 API 非常直观，**5 分钟即可运行第一个智能体**。建议从 [快速开始](https://agently.cn/docs/quickstart.html) 入手，然后根据需求深入查看 [结构化输出](https://agently.cn/docs/output-control/overview.html)、[工作流编排](https://agently.cn/docs/triggerflow/overview.html) 等核心章节。

**Q：如何将基于 Agently 开发的服务部署上线？**  
**A：** 框架本身不绑定部署方式。它提供了完整的异步接口，可以轻松集成到 FastAPI、Django 等任何 Web 框架中。我们提供了开箱即用的 [FastAPI 集成示例](https://github.com/AgentEra/Agently/tree/main/examples/step_by_step/13-auto_loop_fastapi)，涵盖 SSE（流式）、WebSocket 和普通 POST 接口。

**Q：是否有企业版或商业支持？**  
**A：** Agently 核心框架在 **Apache 2.0 开源协议**下完全免费，可放心用于商业项目。如果您所在的企业需要**深度技术合作、定制化支持或培训服务**，欢迎通过 [社区](https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf) 与我们联系。


## 🧭 文档库重要内容导览

- **快速开始与入口**
  - 快速开始: https://agently.cn/docs/quickstart.html
  - 常见模型配置: https://agently.cn/docs/model-settings.html
  - Coding Agent 指南: https://agently.cn/docs/agent-docs.html
- **输出控制（结构化输出）**
  - 概览: https://agently.cn/docs/output-control/overview.html
  - Output Format 语法: https://agently.cn/docs/output-control/format.html
  - ensure_keys: https://agently.cn/docs/output-control/ensure-keys.html
  - Instant 结构化流式: https://agently.cn/docs/output-control/instant-streaming.html
- **结果读取与流式事件**
  - 结果数据与对象: https://agently.cn/docs/model-response/result-data.html
  - 流式返回与事件: https://agently.cn/docs/model-response/streaming.html
- **Session & Memo**
  - 概览: https://agently.cn/docs/agent-extensions/session-memo/
  - 快速开始: https://agently.cn/docs/agent-extensions/session-memo/quickstart.html
- **TriggerFlow 编排**
  - 概览: https://agently.cn/docs/triggerflow/overview.html
  - when 分支: https://agently.cn/docs/triggerflow/when-branch.html
  - if / elif / else: https://agently.cn/docs/triggerflow/if-branch.html
  - match / case: https://agently.cn/docs/triggerflow/match-branch.html
  - batch 并发: https://agently.cn/docs/triggerflow/batch.html
  - for_each: https://agently.cn/docs/triggerflow/for-each.html
  - 运行时流: https://agently.cn/docs/triggerflow/runtime-stream.html
- **工具与扩展**
  - 工具与自动调用: https://agently.cn/docs/agent-extensions/tools.html
  - MCP 接入: https://agently.cn/docs/agent-extensions/mcp.html
  - auto_func: https://agently.cn/docs/agent-extensions/auto-func.html
  - KeyWaiter: https://agently.cn/docs/agent-extensions/key-waiter.html
- **Prompt 管理**: https://agently.cn/docs/prompt-management/overview.html
- **异步与设置**: https://agently.cn/docs/async-support.html / https://agently.cn/docs/settings.html
- **Playbook**: https://agently.cn/docs/agent-systems/overview.html

## 🤝 加入社区

- 交流讨论: https://github.com/AgentEra/Agently/discussions
- 报告问题: https://github.com/AgentEra/Agently/issues
- 微信群: https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf

## 📄 开源协议

Agently 采用 [Apache 2.0](LICENSE) 开源协议，可自由用于商业项目。

---

<p align="center">
  <b>立即开始构建你的生产级 AI 应用 →</b><br>
  <code>pip install -U agently</code>
</p>

<p align="center">
  <sub>有问题？查看 <a href="https://agently.cn/docs">完整文档</a> 或加入 <a href="https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf">社区交流</a></sub>
</p>
