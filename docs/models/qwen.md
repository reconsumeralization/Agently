---
title: Qwen Settings
description: "Agently Qwen (DashScope) setup guide with API key and OpenAI-compatible endpoint configuration."
keywords: "Qwen API setup,DashScope OpenAI compatibility,Agently model settings,AI agent development"
---

# Qwen (DashScope) Settings

## Official links

- Console & keys: https://dashscope.aliyun.com/
- OpenAI compatibility: https://help.aliyun.com/zh/dashscope/developer-reference/openai-file-interface

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "api_key": "YOUR_DASHSCOPE_API_KEY",
  "model": "qwen-turbo"
})
```
