---
title: Export & Versioning
description: "Prompt export docs for v4.0.8.1+: get_json_prompt/get_yaml_prompt and config-driven reload workflow."
keywords: "Agently,Prompt export,get_json_prompt,get_yaml_prompt,load_yaml_prompt"
---

# Export & Versioning

> Applies to: 4.0.8.1+

In `v4.0.8.1+`, prompt export/load capabilities are provided via `ConfigurePromptExtension`.

With default `Agently.create_agent()`, you can directly use:

- `agent.get_json_prompt()`
- `agent.get_yaml_prompt()`
- `agent.load_json_prompt(...)`
- `agent.load_yaml_prompt(...)`

## 1. Why export prompts

In production, prompt snapshots are commonly used for:

- audit/replay of online behavior
- config-center based prompt management
- diffing strategies across environments

## 2. Export layered snapshots (recommended)

```python
json_snapshot = agent.get_json_prompt()
yaml_snapshot = agent.get_yaml_prompt()

print(json_snapshot)
```

Snapshot structure includes:

- `.agent`: long-lived agent-level prompts
- `.request`: request-scoped prompts

This is better for versioning than plain concatenated text.

## 3. Persist snapshots with version metadata

```python
from datetime import datetime

snapshot = agent.get_yaml_prompt()
file_name = f"prompt_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"

with open(file_name, "w", encoding="utf-8") as f:
    f.write(snapshot)
```

Also store alongside:

- model settings
- critical runtime settings
- app/business version tag

## 4. Reload from file or raw string

`load_yaml_prompt` / `load_json_prompt` support:

- file paths
- raw content strings
- `encoding`
- `prompt_key_path` for nested payload extraction
- `mappings` for template variables

```python
agent.load_yaml_prompt("./prompts/customer_support.yaml")

agent.load_json_prompt(
    "./prompt_bundle.json",
    prompt_key_path="scenes.refund",
    mappings={"brand_name": "AgentEra"},
)
```

## 5. Recommended workflow

1. build prompts in code during development
2. export snapshots into versioned config files
3. reload by scenario at runtime with variable mappings

This decouples prompt iteration from release cycles.

## 6. Common issues

### 6.1 Why not `prompt.to_yaml_prompt(...)`

That is legacy style. In `v4.0.8.1+`, prefer `agent.get_yaml_prompt()/get_json_prompt()`.

### 6.2 How to load one nested section from large JSON

Use `prompt_key_path`, for example: `prompt_key_path="flows.refund"`.

### 6.3 Prompt changes seem ignored

Check:

- key path points to expected object
- mapping variables are resolved
- later runtime calls (`agent.input()/agent.system()`) are not overriding loaded values
