import asyncio
from tempfile import TemporaryDirectory

from agently import Agently, TriggerFlow


def build_flow(agent):
    flow = TriggerFlow(name="workspace-loop-foundation")

    async def start(data):
        task = data.input
        await data.async_set_state("task_id", task["task_id"], emit=False)
        data.emit_nowait("ATTEMPT", {"task_id": task["task_id"], "attempt": 1})

    async def run_attempt(data):
        task_id = data.input["task_id"]
        attempt = data.input["attempt"]
        status = "fixed" if attempt >= 2 else "failed"
        observation = {
            "attempt": attempt,
            "status": status,
            "test": "route_fallback",
            "evidence": [
                "provider returned no route candidate"
                if status == "failed"
                else "fallback route selected after patch"
            ],
        }
        observation_ref = await agent.workspace.ingest(
            content=observation,
            collection="observations",
            kind="loop_observation",
            summary=f"route fallback attempt {attempt} {status}",
            scope={"task_id": task_id},
            source={"type": "triggerflow", "step": "run_attempt"},
        )
        context_pack = await agent.workspace.build_context(
            goal="route fallback",
            scope={"task_id": task_id},
            budget={"chars": 1200},
            profile="software_dev",
        )
        decision = {
            "attempt": attempt,
            "next": "stop" if status == "fixed" else "retry_with_patch",
            "context_item_count": len(context_pack["items"]),
        }
        decision_ref = await agent.workspace.ingest(
            content=decision,
            collection="decisions",
            kind="loop_decision",
            summary=f"route fallback decision attempt {attempt}",
            scope={"task_id": task_id},
            source={"type": "triggerflow", "step": "run_attempt"},
        )
        await agent.workspace.link(decision_ref, observation_ref, relation="responds_to")
        checkpoint_ref = await agent.workspace.checkpoint(
            task_id,
            {
                "attempt": attempt,
                "status": status,
                "observation_ref": observation_ref,
                "decision_ref": decision_ref,
            },
            step_id=f"attempt-{attempt}",
        )
        await data.async_set_state("latest_checkpoint_ref", checkpoint_ref, emit=False)
        if status == "fixed":
            latest_checkpoint = await agent.workspace.latest_checkpoint(task_id)
            assert latest_checkpoint is not None
            latest_state = await agent.workspace.get_data(latest_checkpoint)
            link_refs = await agent.workspace.links(relation="responds_to")
            await data.async_set_state(
                "workspace_summary",
                {
                    "latest_status": latest_state["status"],
                    "checkpoint_count": len(await agent.workspace.checkpoint_history(task_id)),
                    "link_count": len(link_refs),
                },
                emit=False,
            )
        else:
            data.emit_nowait("ATTEMPT", {"task_id": task_id, "attempt": attempt + 1})

    flow.to(start)
    flow.when("ATTEMPT").to(run_attempt)
    return flow


async def main():
    with TemporaryDirectory() as temp_dir:
        agent = Agently.create_agent("workspace-loop-example").use_workspace(temp_dir)
        assert agent.workspace is not None
        flow = build_flow(agent)
        execution = flow.create_execution(auto_close=False)
        await execution.async_start({"task_id": "issue-123"})
        state = await execution.async_close()
        summary = state["workspace_summary"]
        print(summary)
        assert summary == {
            "latest_status": "fixed",
            "checkpoint_count": 2,
            "link_count": 2,
        }
        assert agent.workspace.capabilities()["features"]["checkpoint_lookup"] is True


asyncio.run(main())

# Expected key output:
# {'latest_status': 'fixed', 'checkpoint_count': 2, 'link_count': 2}
#
# This is an infrastructure composition smoke, not a model-owned WorkLoop.
# TriggerFlow owns the explicit loop, while Workspace owns durable structured
# observations, decisions, links, checkpoints, and ContextPack recall.
#
# Flow:
# async_start({"task_id": "issue-123"})
#   |
#   v
# start -> emit_nowait("ATTEMPT", {"attempt": 1})
#   |
#   v
# run_attempt(1) -> store failed observation -> build_context -> store decision
#                -> link decision to evidence -> checkpoint -> emit attempt 2
#   |
#   v
# run_attempt(2) -> store fixed observation -> build_context -> store decision
#                -> link decision to evidence -> checkpoint -> summarize
