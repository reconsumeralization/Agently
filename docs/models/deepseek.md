---
title: DeepSeek Settings
description: "Agently DeepSeek setup guide with API keys, model selection, and OpenAI-compatible access."
keywords: "DeepSeek API setup,DeepSeek model config,OpenAI compatible,Agently model settings,AI agent development"
---

# DeepSeek Settings

## Official links

- Website: https://www.deepseek.com/
- Console & keys: https://platform.deepseek.com/
- Models: https://platform.deepseek.com/api-docs/en/pricing

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "YOUR_DEEPSEEK_API_KEY",
  "model": "deepseek-chat"
})
```
