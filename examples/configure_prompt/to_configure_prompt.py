from agently import Agently

agent = Agently.create_agent()

execution = (
    agent.define()
    .role("You are an Agently enhanced agent.")
    .info({"Agently": "Speed up your AI application development. Official website: https://Agently.tech."})
    .create_execution()
    .input("Say hello.")
    .set_agent_prompt("ensure_all_keys", True)  # outermost strict guarantee
    .instruct(
        [
            "Reply {input} politely.",
        ]
    )
    .output(
        {
            "thinking": (
                [(str, "one step of plan")],
                "plans to response",
            ),
            "reply": (str, "reply", True),
            "extra": (
                {
                    "worth_to_remember": (
                        bool,
                        "is {input} and {reply} worth to be remembered that not a normal daily chat?",
                    ),
                    "user_emotion_guess": (str, "how do you thinking user's emotion is going to be after {reply}?"),
                },
                "extra info you need to collect and analysis",
            ),
        }
    )
)

yaml_prompt = execution.get_yaml_prompt()
json_prompt = execution.get_json_prompt()

print("[YAML PROMPT]:")
print(yaml_prompt)
print("[JSON PROMPT]:")
print(json_prompt)

agent_2 = Agently.create_agent()

agent_2.load_yaml_prompt(yaml_prompt)
execution_2 = agent_2.create_execution()
print("[AGENT 2 PROMPT]:")
print(execution_2.get_prompt_text())

# Expected output (deterministic — no model call):
# [YAML PROMPT]: <YAML text of the full prompt>
# [JSON PROMPT]: <JSON text of the full prompt>
# [AGENT 2 PROMPT]: <human-readable prompt text rendered by agent_2 after load>
#
# How it works:
# A complex prompt is built via Agent definition state plus an AgentExecution
# draft, then serialized with execution.get_yaml_prompt() / get_json_prompt().
# load_yaml_prompt() can accept a raw YAML string (not just a file path), so agent_2
# rebuilds the exact same agent + execution prompt from the exported string
# without any file I/O, then create_execution() captures the execution section.
# This round-trip is the canonical way to save and restore agent prompt configurations.
