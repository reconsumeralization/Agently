from agently import Agently

# By default, version > 4.0.7.1 will add current time to prompt automatically
agent = Agently.create_agent()

agent.input("hello")

print("Default:\n", agent.get_prompt_text())


# You can turn off this feature by change settings
agent.set_settings("prompt.add_current_time", False)

print("Turn off:\n", agent.get_prompt_text())
# <Console Logs>
# Default:
#  [current time]: 2026-02-02 23:24:37 Monday

# [INPUT]:
# hello

# [OUTPUT]:
# Turn off:
#  [INPUT]:
# hello

# [OUTPUT]:
