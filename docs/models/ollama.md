---
title: Ollama Local Model Setup
description: "Step-by-step guide to run Ollama local models with Agently, including deployment, model pull, OpenAI-compatible API setup, and troubleshooting."
keywords: "Ollama local model,Ollama deployment,Ollama OpenAI compatible API,local agent,Agently model settings"
---

# Ollama Local Model Setup

This guide is the shortest path for running Agently with a local model through Ollama.

## 1. Install and start Ollama

- Website: <https://ollama.com/>
- Default local endpoint: `http://127.0.0.1:11434`

Quick health check:

```bash
curl http://127.0.0.1:11434/api/tags
```

## 2. Pull a local model

Example:

```bash
ollama pull qwen2.5:7b
```

Suggested sizes:

- Fast test: `qwen2.5:3b`
- Balanced: `qwen2.5:7b`
- Better quality: `qwen2.5:14b` (requires more memory/VRAM)

## 3. Configure Agently via OpenAICompatible

Ollama exposes an OpenAI-compatible API, so you can use the standard `base_url + model` setup.

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
  "base_url": "http://127.0.0.1:11434/v1",
  "api_key": "ollama",  # usually not validated locally; any placeholder string works
  "model": "qwen2.5:7b"
})
```

## 4. Minimal runnable example

```python
from agently import Agently

agent = Agently.create_agent()

result = (
  agent
  .input("Explain AI agents in one sentence.")
  .output({"answer": ("str", "One-sentence answer")})
  .start()
)

print(result)
```

## 5. Troubleshooting

### 5.1 `connection refused` or timeout

- Ensure Ollama is running
- Ensure `base_url` is `http://127.0.0.1:11434/v1`
- Validate with `curl` first

### 5.2 Pull fails or is slow

- Pull model separately with `ollama pull <model>`
- Check network and disk space

### 5.3 Output quality is unstable

- Move to a larger model size
- Strengthen output constraints in prompts
- Use Agently structured outputs for schema stability

## Next

- Output control: [/en/output-control/overview](/en/output-control/overview)
- Prompt management: [/en/prompt-management/overview](/en/prompt-management/overview)
- Response and streaming: [/en/model-response/overview](/en/model-response/overview)
