---
title: GLM Settings
description: "Agently GLM (Zhipu) setup guide with API key and OpenAI-compatible configuration."
keywords: "GLM API setup,Zhipu GLM model config,OpenAI compatible,Agently model settings,AI agent development"
---

# Zhipu GLM Settings

## Official links

- Website: https://open.bigmodel.cn/
- Console & keys: https://open.bigmodel.cn/usercenter/apikeys
- Models: https://open.bigmodel.cn/dev/api#language

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://open.bigmodel.cn/api/paas/v4",
  "api_key": "YOUR_GLM_API_KEY",
  "model": "glm-4"
})
```
