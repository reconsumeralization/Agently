---
title: Groq Settings
description: "Agently Groq setup guide with API key and OpenAI-compatible endpoint configuration."
keywords: "Groq API setup,Groq OpenAI compatibility,Agently model settings,AI agent development"
---

# Groq Settings

## Official links

- Website: https://groq.com/
- Console & keys: https://console.groq.com/keys
- OpenAI compatibility: https://console.groq.com/docs/openai
- Models: https://console.groq.com/docs/models

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://api.groq.com/openai/v1",
  "api_key": "YOUR_GROQ_API_KEY",
  "model": "llama-3.1-70b-versatile"
})
```
