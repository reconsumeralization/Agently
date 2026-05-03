---
title: MiniMax Settings
description: "Agently MiniMax setup guide with API key and OpenAI-compatible model configuration."
keywords: "MiniMax API setup,MiniMax OpenAI compatible,Agently model settings,AI agent development"
---

# MiniMax Settings

## Official links

- Website: https://minimax.io/
- Console & keys: https://platform.minimax.io/
- OpenAI compatibility: https://platform.minimax.io/docs/api-reference/text-openai-api
- Models: https://platform.minimax.io/docs/guides/models-intro

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://api.minimax.io/v1",
  "api_key": "YOUR_MINIMAX_API_KEY",
  "model": "MiniMax-M2.1"
})
```
