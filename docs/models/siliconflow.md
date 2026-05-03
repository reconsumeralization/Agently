---
title: SiliconFlow Settings
description: "Agently SiliconFlow setup guide with API key, model selection, and OpenAI-compatible configuration."
keywords: "SiliconFlow API setup,SiliconCloud model config,OpenAI compatible,Agently model settings,AI agent development"
---

# SiliconFlow Settings

If you are looking for SiliconFlow or SiliconCloud setup, this page gives the standard Agently integration path.

## Official links

- Docs: https://docs.siliconflow.cn/
- Model list: https://docs.siliconflow.cn/reference/chat-completions-1
- API base URL: `https://api.siliconflow.cn/v1`

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://api.siliconflow.cn/v1",
  "api_key": "YOUR_SILICONFLOW_API_KEY",
  "model": "Qwen/Qwen2.5-7B-Instruct"
})
```

## Troubleshooting

### 1. `401 Unauthorized`

- Verify your API key
- Verify the key has access to the selected model

### 2. `model not found`

- Confirm exact model name from the model list
- Check path prefixes and casing (for example `Qwen/...`)

## Related docs

- Model settings overview: [/en/model-settings](/en/model-settings)
- OpenAI request format: [/en/openai-api-format](/en/openai-api-format)

