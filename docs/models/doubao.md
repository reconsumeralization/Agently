---
title: Doubao Settings
description: "Agently Doubao (Volcengine Ark) setup guide with endpoint ID, API key, and OpenAI-compatible configuration."
keywords: "Doubao API setup,Volcengine Ark endpoint,Doubao model config,OpenAI compatible,Agently model settings"
---

# Doubao (Volcengine Ark) Settings

## Official links

- Console & keys: https://console.volcengine.com/ark/
- Endpoints: https://console.volcengine.com/ark/region:ark+cn-beijing/endpoint
- Model enablement: https://console.volcengine.com/ark/region:ark+cn-beijing/openManagement

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://ark.cn-beijing.volces.com/api/v3",
  "api_key": "YOUR_DOUBAO_API_KEY",
  # model is the endpoint ID from Volcengine console
  "model": "ep-xxxx"
})
```
