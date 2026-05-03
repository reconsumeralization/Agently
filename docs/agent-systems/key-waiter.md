---
title: KeyWaiter Playbook
description: "Agently agent systems playbook KeyWaiter Playbook with practical patterns for production AI applications."
keywords: "Agently,agent systems,engineering playbook,AI applications,KeyWaiter Playbook"
---

# KeyWaiter Playbook

## Scenario
Compliance/risk workflows need **early field triggers** before the full response completes.

## Capability (key traits)
- **KeyWaiter** for field‑level listeners
- **instant** structured streaming

## Operations
1) Define output schema.
2) Register `when_key` handlers.

## Full code

```python
from agently import Agently

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "model_type": "chat",
    },
).set_settings("request_options", {"temperature": 0.2}).set_settings("debug", False)

agent = Agently.create_agent()

(
    agent.system(
        "你是合规预审助手，先给出风险，再给出处理建议。"
    )
    .input(
        "我要在官网放一个‘七天包退’的宣传页，文案包含："
        "‘100% 保本、无任何风险’。帮我判断风险并给出建议。"
    )
    .output(
        {
            "risk": (str, "主要合规风险"),
            "decision": (str, "处理建议"),
            "rewrite": (str, "替代文案"),
        }
    )
    .when_key("risk", lambda v: print("RISK:", v))
    .when_key("decision", lambda v: print("DECISION:", v))
    .when_key("rewrite", lambda v: print("REWRITE:", v))
    .start_waiter()
)
```

## Real output

```text
RISK: 广告宣传可能存在误导，容易使消费者误解为投资绝对保本且无风险。
DECISION: 建议修改文案，避免使用绝对化词语，并明确表示可能存在的投资风险。
REWRITE: 我们承诺在七天内如果您对产品不满意，可以包退。请理解所有金融和投资项目均有可能带来收益同时也伴随着一定的风险。
```

## Validation
- Key fields are triggered before completion.
- Field paths are stable and consistent.
