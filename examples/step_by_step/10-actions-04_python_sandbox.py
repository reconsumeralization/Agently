from agently import Agently

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
    },
)

agent = Agently.create_agent()
agent.set_action_loop(max_rounds=4)


## Python sandbox action — let the model write and execute Python code
#
# Instead of registering a fixed function, a sandbox action lets the model
# generate arbitrary Python code that Agently runs through a Docker-backed profile.
# The model must print its final answer as JSON or bounded text.
# This is useful for one-off computations, data transformations, or any task
# where the exact code needed depends on the input.

agent.enable_python(
    action_id="run_python",
    desc=(
        "Execute Python through the Workspace-bound CodeExecution chain. "
        "Always print the final answer as JSON before the code ends."
    ),
    expose_to_model=True,
    sandbox="auto",
)


def demo_python_sandbox():
    agent.use_actions("run_python")
    turn = agent.input(
        "I have five product prices: 29.99, 14.50, 89.00, 5.25, 49.75. "
        "Use the Python sandbox to compute: the total, the average, and the price range "
        "(max minus min). Return all three values."
    )
    records = agent.get_action_result(prompt=turn.prompt)
    print("[action records]", records)
    result = turn.get_result()
    print(result.get_text())


# demo_python_sandbox()


def demo_python_sandbox_sorting():
    agent.use_actions("run_python")
    turn = agent.input(
        "Sort these words alphabetically and count unique first letters: "
        "['mango', 'apple', 'banana', 'avocado', 'blueberry', 'melon', 'cherry']. "
        "Use the Python sandbox."
    )
    records = agent.get_action_result(prompt=turn.prompt)
    print("[action records]", records)
    result = turn.get_result()
    print(result.get_text())


# demo_python_sandbox_sorting()


# Expected output (demo_python_sandbox — values are deterministic, text varies):
# [action records] [ActionResult(action_id='run_python', model_digest=..., artifact_refs=[...])]
# Total: 188.49
# Average: 37.70
# Price range (max - min): 83.75
#
# How it works:
# enable_python() creates a CodeExecution Action that accepts source_code from the
# model. The immutable source bundle is materialized through TaskWorkspace and runs
# through the selected Docker provider with network disabled by default. Bounded
# stdout and declared output artifacts become the action result.
# The model writes computation as Python code instead of calling a fixed function —
# useful when the exact logic depends on the specific question asked.
# The ActionResult contains model_digest (a summary the model can read) and
# artifact_refs (pointers to the stored code and stdout for audit purposes).
#
# Flow:
# agent.use_actions("run_python")
# turn = agent.input("Compute total, average, range for [29.99, 14.50, ...]")
# agent.get_action_result(prompt=turn.prompt)
#   model plans: run_python(code="""
#       prices = [29.99, 14.50, 89.00, 5.25, 49.75]
#       result = {'total': sum(prices), 'average': sum(prices)/len(prices),
#                 'range': max(prices) - min(prices)}
#   """)
#   Docker-backed Python profile runs the code
#   result = {'total': 188.49, 'average': 37.698, 'range': 83.75}
#   |
#   v
# agent.get_result()
#   model reads model_digest and replies with all three computed values
