---
title: Gemini Settings
description: "Agently Gemini setup guide with Google API keys and OpenAI-compatible endpoint configuration."
keywords: "Gemini API setup,Google Gemini OpenAI compatible,Agently model settings,AI agent development"
---

# Gemini Settings

## Official links

- Website: https://ai.google.dev/
- API keys: https://aistudio.google.com/apikey
- OpenAI compatibility: https://ai.google.dev/gemini-api/docs/openai
- Models: https://ai.google.dev/gemini-api/docs/models

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
  "api_key": "YOUR_GEMINI_API_KEY",
  "model": "gemini-3-flash-preview"
})
```
