import asyncio
from pprint import pprint

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData


async def main():
    flow = TriggerFlow(name="example-triggerflow-managed-python")
    agent = Agently.create_agent()
    agent.enable_python(
        action_id="managed_python",
        expose_to_model=False,
        sandbox="trusted_local",
    )

    async def calculate(data: TriggerFlowRuntimeData):
        result = await agent.action.async_execute_action(
            "managed_python",
            {"source_code": f"print(40 + {int(data.value)})\n"},
        )
        assert result.get("status") == "success", result
        data.state.set("answer", int(result["data"]["stdout"].strip()))

    flow.to(calculate)

    result = await flow.async_start(2)

    print("[TRIGGERFLOW_RESULT]")
    pprint(result)
    assert result == {"answer": 42}

    execution_handles = Agently.execution_resource.list(scope="execution")
    print("[EXECUTION_HANDLES_AFTER_RELEASE]")
    pprint(execution_handles)
    assert execution_handles == []


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output:
# [TRIGGERFLOW_RESULT] prints {"answer": 42}.
# [EXECUTION_HANDLES_AFTER_RELEASE] prints [] after the action-call resource is released.

# How it works:
# TriggerFlow owns orchestration. The Action Runtime owns code execution.
# The chunk calls a canonical CodeExecution Action; TaskWorkspace materializes the
# immutable source bundle before the selected trusted-local provider executes it.
# The action-call-scoped handle and Workspace grant are released after the call.
#
# Flow:
# async_start(2)
#   v
# calculate: agent.action.async_execute_action("managed_python", {"source_code": "print(40 + 2)"})
#   Workspace bundle -> trusted_local provider -> stdout "42"
#   data.state.set("answer", 42)
#   |
#   v
# async_close() -> {"answer": 42}; action-call handle already released
