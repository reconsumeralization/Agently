---
title: ERNIE Settings
description: "Agently ERNIE (Wenxin/Qianfan) setup guide with API keys and OpenAI-compatible integration."
keywords: "ERNIE API setup,Wenxin model config,Qianfan API key,OpenAI compatible,Agently model settings"
---

# ERNIE (Wenxin) Settings

## Official links

- Product page: https://cloud.baidu.com/product/wenxinworkshop
- Qianfan console: https://console.bce.baidu.com/qianfan/overview
- API keys: https://console.bce.baidu.com/iam/#/iam/apikey/list
- OpenAI compatibility: https://ai.baidu.com/ai-doc/WENXINWORKSHOP/2m3fihw8s

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://qianfan.baidubce.com/v2",
  "api_key": "YOUR_QIANFAN_API_KEY",
  "model": "ERNIE-4.0-8K"
})
```
