import json
from pprint import pprint
from typing import Any, cast

from agently import Agently


ACTION_ID = "calculate_stats"


def build_agent():
    agent = Agently.create_agent()
    agent.enable_python(action_id=ACTION_ID, expose_to_model=False, sandbox="trusted_local")
    return agent


def main():
    agent = build_agent()
    result = agent.action.execute_action(
        ACTION_ID,
        {
            "source_code": "\n".join(
                [
                    "import json",
                    "numbers = [15, 23, 42, 8, 12]",
                    "result = {",
                    "    'average': sum(numbers) / len(numbers),",
                    "    'count': len(numbers),",
                    "    'max_minus_min_gap': max(numbers) - min(numbers),",
                    "}",
                    "print(json.dumps(result))",
                ]
            )
        },
    )

    print("[ACTION_RESULT]")
    pprint(result)

    result_data = cast(dict[str, Any], result.get("data"))
    assert result.get("status") == "success"
    assert json.loads(result_data["stdout"]) == {
        "average": 20.0,
        "count": 5,
        "max_minus_min_gap": 34,
    }

    action_call_handles = Agently.execution_resource.list(scope="action_call")
    print("[ACTION_CALL_HANDLES_AFTER_RELEASE]")
    pprint(action_call_handles)
    assert action_call_handles == []


if __name__ == "__main__":
    main()

# Expected key output:
# [ACTION_RESULT] has status="success" and JSON stdout equals
# {"average": 20.0, "count": 5, "max_minus_min_gap": 34}.
# [ACTION_CALL_HANDLES_AFTER_RELEASE] prints [].

# How it works:
# agent.enable_python(..., sandbox="trusted_local") registers the canonical
# Workspace-bound CodeExecution action with the explicitly unsafe local provider.
# execute_action() calls it directly with source_code and reads bounded stdout.
# After the call, Agently.execution_resource.list(scope="action_call") returns []
# because action-call-scoped handles are released automatically when the call ends.
#
# Flow:
# agent.enable_python(action_id=ACTION_ID, expose_to_model=False, sandbox="trusted_local")
#   |
#   v
# agent.action.execute_action(ACTION_ID, {"source_code": "..."})
#   | (no model call)
#   v
# TaskWorkspace materializes the immutable bundle -> trusted_local runs it -> JSON stdout
#   |
#   v
# handle released (scope="action_call") -> list(scope="action_call") == []
