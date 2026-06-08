import os
from agently import Agently

current_dir = os.path.dirname(os.path.abspath(__file__))
yaml_prompt_path = os.path.join(current_dir, "yaml_prompt.yaml")

agent = Agently.create_agent()
agent.load_yaml_prompt(
    yaml_prompt_path,
    mappings={
        "in_value_placeholder": "IN VALUE!",
        "key_name_placeholder": "KEY_NAME",
        "only_value_placeholder": [
            "THIS",
            "IS",
            "ONLY",
            "VALUE",
            "PLACEHOLDER",
        ],
    },
)
execution = agent.create_execution()

print("AGENT PROMPT:", agent.agent_prompt.get())
print("EXECUTION PROMPT:", execution.request_prompt.get(inherit=False))
print("AGENT PENDING PROMPT AFTER CAPTURE:", agent.request.prompt.get(inherit=False))

# Expected output (deterministic — no model call):
# AGENT PROMPT: {...}        — persistent .agent / $... prompt slots
# EXECUTION PROMPT: {...}    — .execution and top-level prompt slots for one run
# AGENT PENDING PROMPT AFTER CAPTURE: {} — create_execution() consumed the pending draft
#
# How it works:
# load_yaml_prompt() reads the YAML prompt file, fills in ${placeholder} tokens from
# mappings= with the provided values, and applies the result to the agent's prompt store.
# The YAML format mirrors the JSON format but is more readable for multi-field
# prompts. .agent writes persistent Agent definition prompt, while .execution
# writes a one-run prompt draft that create_execution() captures and clears.
