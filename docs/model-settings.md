---
title: Common Model Settings
description: "Agently model settings guide with unified OpenAI-compatible setup across providers, including SiliconFlow and Ollama local models."
keywords: "Agently model settings,OpenAI compatible,SiliconFlow setup,OpenAI API format,Ollama local model,AI agent framework"
---

# Common Model Settings

When integrating multiple providers, you usually want a single configuration pattern and low switching cost. Agently v4 uses **OpenAICompatible** for all providers with `base_url + api_key + model`.

## General template

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://api.example.com/v1",
  "api_key": "YOUR_API_KEY",
  "model": "your-model-name"
})
```

## Model index

- Global: [OpenAI](/en/models/openai) / [Claude](/en/models/claude) / [Gemini](/en/models/gemini) / [Groq](/en/models/groq)
- China: [DeepSeek](/en/models/deepseek) / [Qwen](/en/models/qwen) / [ERNIE](/en/models/ernie) / [Kimi](/en/models/kimi) / [Doubao](/en/models/doubao) / [SiliconFlow](/en/models/siliconflow) / [MiniMax](/en/models/minimax) / [GLM](/en/models/glm)
- Local deployment: [Ollama Local Model](/en/models/ollama)
- API compatibility: [OpenAI API Request Format Guide](/en/openai-api-format)
