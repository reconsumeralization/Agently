---
title: Kimi Settings
description: "Agently Kimi (Moonshot) setup guide with API key, base URL, and OpenAI-compatible configuration."
keywords: "Kimi API setup,Moonshot model config,OpenAI compatible,Agently model settings,AI agent development"
---

# Kimi (Moonshot) Settings

## Official links

- Website: https://www.moonshot.cn/
- Console & keys: https://platform.moonshot.cn/
- Models: https://platform.moonshot.cn/docs/intro#models

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://api.moonshot.cn/v1",
  "api_key": "YOUR_KIMI_API_KEY",
  "model": "moonshot-v1-8k"
})
```
