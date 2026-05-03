---
title: Claude Settings
description: "Agently Claude (Anthropic) setup guide with API key, base URL, and OpenAI-compatible configuration."
keywords: "Claude API setup,Anthropic API key,OpenAI compatible,Agently model settings,AI agent development"
---

# Claude (Anthropic) Settings

## Official links

- Website: https://www.anthropic.com/
- Console & keys: https://console.anthropic.com/
- Models: https://platform.claude.com/docs/en/about-claude/models/overview
- OpenAI SDK compatibility: https://platform.claude.com/docs/en/api/openai-sdk

## OpenAICompatible setup

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "https://api.anthropic.com/v1",
  "api_key": "YOUR_CLAUDE_API_KEY",
  "model": "claude-3-5-sonnet-20240620"
})
```
