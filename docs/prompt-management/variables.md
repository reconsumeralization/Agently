---
title: Variables and Templating
description: "Agently prompt engineering guide Variables and Templating covering layered prompts, config-driven prompts, and mappings."
keywords: "Agently,prompt engineering,prompt management,AI agent development,Variables and Templating"
---

# Variables and Templating

Variables are not just about passing arguments. In production you often want a stable template while swapping data from APIs, forms, and logs. Variable mapping makes that reuse practical.

## Use placeholders in templates

```yaml
.request:
  input: Write one-line positioning for ${product_name}.
  instruct: Tone: ${tone}
```

## Pass mappings on load

```python
from agently import Agently

agent = Agently.create_agent()
agent.load_yaml_prompt(
  "prompts/positioning.yaml",
  mappings={
    "product_name": "Agently",
    "tone": "professional and concise",
  },
)
```

## Substitute in code

```python
agent.set_request_prompt(
  "input",
  "Write one-line positioning for ${product_name}.",
  mappings={"product_name": "Agently"},
)
```

Placeholders are replaced during loading and setting, which keeps templates reusable across products and decoupled from business logic.
