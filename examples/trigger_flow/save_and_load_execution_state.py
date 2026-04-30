import asyncio
from pathlib import Path

from agently import TriggerFlow

flow = TriggerFlow(name="execution-state-resume-demo")


async def prepare_request(data):
    await data.async_set_state(
        "request",
        {
            "topic": data.input,
            "status": "waiting_feedback",
        },
    )


async def resume_with_feedback(data):
    request = data.get_state("request", {}) or {}
    await data.async_set_state(
        "final",
        {
            "topic": request.get("topic"),
            "feedback": data.input,
            "status": "done",
        },
    )


flow.to(prepare_request)
flow.when("UserFeedback").to(resume_with_feedback)


async def main():
    state_file = Path(__file__).with_name("execution_state_checkpoint.json")
    execution = flow.create_execution(auto_close=False)
    await execution.async_start("refund order #A1001")
    execution.save(state_file)

    restored_execution = flow.create_execution(auto_close=False)
    restored_execution.load(state_file)
    await restored_execution.async_emit(
        "UserFeedback",
        {
            "approved": True,
            "note": "Customer uploaded a valid receipt.",
        },
    )
    state = await restored_execution.async_close()
    state_file.unlink(missing_ok=True)
    assert state["final"]["status"] == "done"
    print(state["final"])


asyncio.run(main())
