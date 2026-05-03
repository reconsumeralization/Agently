---
title: OpenAI Settings
description: "Agently OpenAI setup guide with API keys, model selection, and OpenAI-compatible request configuration."
keywords: "OpenAI API format,OpenAI request format,OpenAI API schema,Agently OpenAI setup,AI agent development"
---

# OpenAI Settings

## Official links

- Website: https://openai.com/
- API keys: https://platform.openai.com/api-keys
- Models: https://platform.openai.com/docs/models/overview

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://api.openai.com/v1",
  "api_key": "YOUR_OPENAI_API_KEY",
  "model": "gpt-4o-mini"
})
```

## API format reference

If you are looking for OpenAI request format details and compatibility notes:

- [OpenAI API Request Format Guide](/en/openai-api-format)
