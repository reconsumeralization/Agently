---
title: "Config Prompts: YAML/JSON"
description: "Agently prompt engineering guide Config Prompts: YAML/JSON covering layered prompts, config-driven prompts, and mappings."
keywords: "Agently,prompt engineering,prompt management,AI agent development,Config Prompts: YAML/JSON"
---

# Config Prompts: YAML/JSON

Config prompts are about decoupling. Prompt content evolves faster than code, so keeping it in YAML/JSON makes it versionable, reviewable, and hot‑swappable.

In team settings or fast‑moving products, you usually want prompts to change without code releases. Config prompts make that possible. Typical cases include collaboration, frequent prompt edits, and A/B rollout.

## Write a YAML file

```yaml
.agent:
  system: You are a rigorous technical writer.
  developer: Follow Markdown formatting rules.

.request:
  input: Explain recursion and give 2 tips.
  output:
    Explanation:
      $type: str
      $desc: One-line explanation
    Tips:
      - $type: str
        $desc: Short tip
```

`output` supports `$type/$desc` (or `.type/.desc`) to describe Agently Output Format.

## Design intent

1. Prompts are product assets, not code literals  
2. Separate ownership between prompt authors and engineers  
3. Enable fast iteration and rollback

## Load in code

```python
from agently import Agently

agent = Agently.create_agent()
agent.load_yaml_prompt("prompts/recursion.yaml")
```

## Load a subtree only

```python
agent.load_yaml_prompt("prompts/recursion.yaml", prompt_key_path=".request")
```

## Use `$` prefix for Agent Prompt

This is equivalent to `.agent`:

```yaml
$system: You are a rigorous technical writer.
$developer: Follow Markdown formatting rules.
input: Explain recursion.
```
