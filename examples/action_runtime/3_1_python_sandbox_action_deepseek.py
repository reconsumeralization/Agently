from _shared import create_deepseek_agent, print_action_results, print_response


agent = create_deepseek_agent(
    "Use the python sandbox action whenever exact computation or small data processing is needed."
)

agent.enable_python(
    action_id="python_table_stats",
    desc=(
        "Execute Python through the Workspace-bound CodeExecution chain. "
        "Always print the final answer as JSON."
    ),
    expose_to_model=True,
)


if __name__ == "__main__":
    agent.use_actions("python_table_stats")
    turn = agent.input(
        "Use the python sandbox action to compute the average and the max-minus-min gap "
        "for [15, 23, 42, 8, 12]. Return both numbers."
    )
    records = agent.get_action_result(prompt=turn.prompt)
    print_action_results(records)
    result = turn.get_result()
    print_response(result)

# Expected key output after configuring DeepSeek:
# [ACTION_RECORDS] includes a successful python_table_stats call.
# The ActionResult has model_digest and artifact_refs for the Python code/output.
# The calculated values are average=20.0 and max-minus-min gap=34.

# How it works:
# agent.enable_python() creates a CodeExecution action that accepts source_code from
# the model, materializes an immutable bundle through TaskWorkspace, and executes it
# with the selected isolated provider. Bounded stdout and declared artifacts form the
# action result; the default Docker policy disables network access.
#
# Flow:
# agent.input("compute average and gap for [15,23,42,8,12]")
#   |
#   v
# model plans: python_table_stats(source_code="...print(json.dumps({'avg':...,'gap':...}))")
#   |
#   v
# Workspace-bound CodeExecution runs Python -> average=20.0, gap=34
#   |
#   v
# ActionResult(model_digest=..., artifact_refs=[...])
#   |
#   v
# model reply: "average=20.0, max-minus-min gap=34"
