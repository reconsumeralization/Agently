---
title: Ticket Triage Playbook
description: "Agently agent systems playbook Ticket Triage Playbook with practical patterns for production AI applications."
keywords: "Agently,agent systems,engineering playbook,AI applications,Ticket Triage Playbook"
---

# Ticket Triage Playbook

## Scenario
Support/ops tickets need fast **classification**, **priority**, and **next action**.

## Capability (key traits)
- **Structured output** for stable fields
- **ensure_keys** to guarantee critical fields

## Operations
1) Define schema with `output()`.
2) Enforce keys with `ensure_keys`.

## Full code

```python
import json
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

result = (
    agent.system(
        "你是客服工单分诊助手。仅输出结构化字段，字段要简短、可执行。"
    )
    .input(
        "我在 App 里购买会员被重复扣款两次，但订单里只显示一次。\n"
        "我想尽快退款，并确认后续不会再扣。"
    )
    .output(
        {
            "issue_type": (str, "问题类型：支付/退款/物流/账号/其它"),
            "priority": (str, "P0/P1/P2"),
            "summary": (str, "一句话摘要"),
            "user_requests": [(str, "用户诉求")],
            "next_action": (str, "建议动作"),
            "missing_info": [(str, "缺失信息")],
        }
    )
    .start(
        ensure_keys=[
            "issue_type",
            "priority",
            "summary",
            "user_requests[*]",
            "next_action",
        ],
        max_retries=1,
        raise_ensure_failure=False,
    )
)

print(json.dumps(result, ensure_ascii=False, indent=2))
```

## Real output

```text
{
  "issue_type": "支付",
  "priority": "P1",
  "summary": "App购买会员被重复扣款，需退款并确认不再重复扣费。",
  "user_requests": [
    "尽快退款",
    "确认后续不会再重复扣费"
  ],
  "next_action": "核查订单情况并处理退款请求",
  "missing_info": [
    "具体订单号"
  ]
}
```

## Validation
- Required fields exist (`ensure_keys`).
- Fields can drive downstream workflows.
