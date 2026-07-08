from agently import Agently

agent = Agently.create_agent()

# Current-time prompt injection is controlled by prompt.add_current_time.
# The default is False; enable it explicitly when the model needs wall-clock context.
agent.set_settings("prompt.add_current_time", True)
with_time_execution = agent.input("hello")

print("Turn on:\n", with_time_execution.get_prompt_text())


agent.set_settings("prompt.add_current_time", False)
without_time_execution = agent.input("hello")

print("Turn off:\n", without_time_execution.get_prompt_text())
# <Console Logs>
# Turn on:
#  [current time]: 2026-02-02 23:24:37 Monday

# [INPUT]:
# hello

# [OUTPUT]:
# Turn off:
#  [INPUT]:
# hello

# [OUTPUT]:

# Expected output (deterministic — no model call, only prompt inspection):
# Turn on:
#  [current time]: <current timestamp>
#
#  [INPUT]:
#  hello
#
#  [OUTPUT]:
# Turn off:
#  [INPUT]:
#  hello
#
#  [OUTPUT]:
#
# How it works:
# Agently prepends a "[current time]: <timestamp>" line only when
# prompt.add_current_time is True. agent.input(...) returns an AgentExecution;
# execution.get_prompt_text() serializes that one-run prompt without sending it
# to a model, so the output is deterministic.
# set_settings("prompt.add_current_time", False) removes the timestamp line for
# subsequent executions.
