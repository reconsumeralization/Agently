---
title: 项目结构
description: 非琐碎 Agently 项目的推荐目录布局。
keywords: Agently, 项目结构, settings, prompt, 工作流, FastAPI
---

# 项目结构

> 语言：[English](../../en/start/project-framework.md) · **中文**

一旦超出单脚本，拆分真实关注点通常会有帮助：

- 设置放在文件，不需要为不同环境改代码。
- Prompt 放在 YAML / JSON，可被非工程同事 review。
- 业务代码不直接 import key 或 model name。

## 推荐布局

```text
my-agently-app/
  pyproject.toml              # 或 requirements.txt
  .env                        # 本地敏感配置（gitignore）
  settings.yaml               # 全局模型与运行时设置
  prompts/
    summarize.yaml            # 一份 prompt 一个文件
    triage.yaml
  flows/
    triage.py                 # TriggerFlow 定义
  app/
    api.py                    # FastAPI 入口
    agents.py                 # agent 工厂
    actions.py                # action / tool 注册
    main.py
  tests/
    test_triage_flow.py
```

只需 `settings.yaml` 与 `prompts/*` 这条主线就能跑起来，其余是可扩展骨架，
不是必须逐项完成的目录清单。

## 降低信息检索成本

尽量降低人类与 Coding Agent 为理解当前行为所需的跨文件检索次数和嵌套深度。只有
新边界确有实际复用价值，或拥有独立维护、版本化的 contract 时，才把只使用一次的
schema、常量、helper、class 或 wrapper 拆到其他位置；没有这两类收益的形式化拆分
属于过度设计。

## settings.yaml

```yaml
plugins:
  ModelRequester:
    OpenAICompatible:
      base_url: ${ENV.OPENAI_BASE_URL}
      api_key: ${ENV.OPENAI_API_KEY}
      model: ${ENV.OPENAI_MODEL}
debug: false
```

启动时加载：

```python
from agently import Agently

Agently.load_settings("yaml_file", "settings.yaml", auto_load_env=True)
```

`auto_load_env=True` 先读 `.env`，再解析 `${ENV.*}`。

## Prompt 写在文件里

```yaml
# prompts/summarize.yaml
.execution:
  instruct: |
    你是一个简洁的编辑，保持事实不变。
  output:
    title:
      $type: str
      $ensure: true
    body:
      $type: str
      $ensure: true
```

加载与使用：

```python
from agently import Agently

agent = Agently.create_agent().load_yaml_prompt("prompts/summarize.yaml")
result = agent.input(article_text).start()
```

`$ensure: true` 是 `(type, "desc", True)` 元组第三槽的 YAML 形式——见 [Schema as Prompt](../requests/schema-as-prompt.md)。Legacy `$default` 已不再支持。

## 多处复用时使用 Agent 工厂

多个调用点复用同一份有 owner 的配置时，可以集中创建：

```python
# app/agents.py
from agently import Agently


def make_summarizer():
    return Agently.create_agent().load_yaml_prompt("prompts/summarize.yaml")
```

如果只有一个调用点，不要仅仅为了隐藏一次 `load_yaml_prompt(...)` 调用就创建 factory；
等到它确实拥有复用、policy、lifecycle 或其他稳定边界时再抽取。

## Flow 放在哪里

每个 TriggerFlow 一个独立模块，放在 `flows/`。Service 代码 import flow 对象再创建 execution，flow 定义本身不耦合 FastAPI / 队列等基础设施。见 [TriggerFlow 概览](../triggerflow/overview.md)。

## Action 放在哪里

如果 agent 需要调工具 / MCP / sandbox，把注册逻辑放在 agent 工厂旁或独立的 `actions.py`。新代码使用 action-first 入口（`@agent.action_func`、`agent.use_actions(...)`）；`tool_func` / `use_tools` / `use_mcp` / `use_sandbox` 仍然可用，但定位为兼容入口，详见 [Action Runtime](../actions/action-runtime.md)。

## 另见

- [设置](settings.md)
- [Prompt 管理](../requests/prompt-management.md)
- [Schema as Prompt](../requests/schema-as-prompt.md)
- [TriggerFlow 概览](../triggerflow/overview.md)
- [FastAPI 服务封装](../services/fastapi.md)
