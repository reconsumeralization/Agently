from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from agently import Agently
from agently.core import PluginManager
from agently.core.application.AgentExecution import AgentExecutionLimitExceeded
from agently.types.data import AgentlyRequestData
from agently.types.options import ExecutionOptions, SkillsRouteOptions
from agently.utils import DataFormatter
from agently.utils import Settings


class MockAgentExecutionRequester:
    name = "MockAgentExecutionRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}
    requests: list[str] = []

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"messages": self.prompt.to_messages(), "output": self.prompt.get("output")},
            request_options={"stream": True},
            request_url="mock://agent-execution",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        MockAgentExecutionRequester.requests.append(json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False))
        yield "message", json.dumps(
            {
                "answer": f"ok-{ len(MockAgentExecutionRequester.requests) }",
                "status": "ready",
            },
            ensure_ascii=False,
        )

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, object], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
                yield "delta", str(data)
        yield "done", response_text


class MockAgentExecutionActionRequester(MockAgentExecutionRequester):
    name = "MockAgentExecutionActionRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "next_action" in text and "execution_commands" in text:
            if "done_plans: []" in text:
                payload = {
                    "next_action": "execute",
                    "execution_commands": [
                        {
                            "purpose": "Run allowlisted echo command",
                            "action_id": "echo_cli",
                            "action_input": {"cmd": "echo allowed action-output"},
                            "todo_suggestion": "Respond after echo completes.",
                        }
                    ],
                }
            else:
                payload = {"next_action": "response", "execution_commands": []}
        elif "[ACTION RESULTS]" in text:
            payload = {"answer": "used-action", "status": "ready"}
        else:
            payload = {"answer": "plain-text-delta", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


def _create_agent(name: str = "agent-execution-step-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockAgentExecutionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_action_agent(name: str = "agent-execution-action-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockAgentExecutionActionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _write_skill(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        """---
name: Task Step Skill
description: Use for task step contract smoke tests.
---

# Task Step Skill

Return a short structured answer for the task step contract.
""",
        encoding="utf-8",
    )


def test_execution_options_validate_known_route_schema():
    with pytest.raises(ValueError):
        ExecutionOptions.model_validate({"routes": {"skills": {"unknown": True}}})


@pytest.mark.asyncio
async def test_agent_execution_one_turn_keeps_compatibility_mode_and_stream_meta():
    MockAgentExecutionRequester.requests = []
    agent = _create_agent("one-turn-compat")
    execution = (
        agent
        .input("classify this ticket")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution()
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant") if item.is_complete]
    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["answer"] == "ok-1"
    assert meta["execution_mode"] == "one_turn"
    assert meta["limits"]["max_model_requests"] is None
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 1
    assert any(item.path == "route.selected" for item in stream_items)
    assert all(item.meta["execution_id"] == meta["execution_id"] for item in stream_items if item.meta)
    assert all(item.meta["execution_mode"] == "one_turn" for item in stream_items if item.meta)


@pytest.mark.asyncio
async def test_agent_execution_turn_alias_normalizes_to_one_turn():
    agent = _create_agent("turn-alias")
    execution = (
        agent
        .input("legacy turn alias")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(mode="turn")
    )

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["answer"] == "ok-1"
    assert execution.mode == "one_turn"
    assert meta["execution_mode"] == "one_turn"


@pytest.mark.asyncio
async def test_agent_execution_select_route_is_reused_by_start():
    agent = _create_agent("route-reuse")
    execution = (
        agent
        .input("route reuse")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution()
    )

    first_route = await execution.select_route()
    second_route = await execution.select_route()
    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert first_route == second_route
    assert data["answer"] == "ok-1"
    assert meta["route"]["selected_route"] == first_route[0]
    assert meta["route"]["reusable"] is True


@pytest.mark.asyncio
async def test_agent_execution_model_request_exposes_action_logs_and_artifacts():
    agent = _create_action_agent("action-log-exposure")
    agent.set_action_loop(max_rounds=2, timeout=5)
    agent.enable_shell(commands=["echo allowed"], action_id="echo_cli")
    execution = (
        agent
        .input("use echo action")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            mode="task_step",
            lineage={"task_id": "action-task", "iteration_id": "iter-1", "step_id": "echo"},
            limits={"max_model_requests": None},
        )
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    action_items = [item for item in stream_items if item.path == "actions.echo_cli"]
    assert data["answer"] == "used-action"
    assert meta["logs"]["model_response_ids"]
    assert meta["logs"]["action_logs"][0]["action_id"] == "echo_cli"
    assert meta["logs"]["action_logs"][0]["route"] == "model_request"
    assert meta["logs"]["artifact_refs"]
    assert action_items
    assert action_items[0].meta["lineage"]["task_id"] == "action-task"


@pytest.mark.asyncio
async def test_agent_execution_plain_text_model_request_streams_model_delta():
    agent = _create_action_agent("plain-text-stream")
    execution = agent.input("plain text route").create_execution()

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    meta = await execution.async_get_meta()

    assert any(item.path == "model.delta" and item.event_type == "delta" for item in stream_items)
    assert any(item.path == "model.text" for item in stream_items)
    assert meta["route"]["selected_route"] == "model_request"


@pytest.mark.asyncio
async def test_agent_execution_task_step_meta_lineage_and_limit_success():
    MockAgentExecutionRequester.requests = []
    agent = _create_agent("task-step-success")
    execution = (
        agent
        .input("produce one bounded answer")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            mode="task_step",
            lineage={"task_id": "task-1", "iteration_id": "iter-1", "step_id": "draft"},
            limits={"max_model_requests": 1},
        )
    )

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["answer"] == "ok-1"
    assert meta["execution_mode"] == "task_step"
    assert meta["lineage"]["task_id"] == "task-1"
    assert meta["limits"]["max_model_requests"] == 1
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 1


@pytest.mark.asyncio
async def test_agent_execution_task_step_blocks_when_direct_model_budget_exceeded():
    agent = _create_agent("task-step-direct-budget")
    execution = (
        agent
        .input("this should exceed budget before provider call")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            mode="task_step",
            lineage={"task_id": "budget-task", "iteration_id": "iter-1", "step_id": "direct"},
            limits={"max_model_requests": 0},
        )
    )

    with pytest.raises(AgentExecutionLimitExceeded):
        await execution.async_get_data()

    meta = await execution.async_get_meta()
    assert meta["status"] == "blocked"
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 0
    assert meta["diagnostics"]["limit_events"][0]["limit_name"] == "max_model_requests"


@pytest.mark.asyncio
async def test_agent_execution_task_step_budget_covers_dynamic_task_model_tasks():
    agent = _create_agent("task-step-dynamic-budget")
    execution = (
        agent
        .use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "task-step-budget-dag",
                "task_schema_version": "task_dag/v1",
                "tasks": [
                    {
                        "id": "first",
                        "kind": "model",
                        "inputs": {"output_schema": {"answer": (str, "answer", True)}},
                    },
                    {
                        "id": "second",
                        "kind": "model",
                        "depends_on": ["first"],
                        "inputs": {"output_schema": {"answer": (str, "answer", True)}},
                    },
                ],
                "semantic_outputs": {"final": "second"},
            },
            timeout=3,
        )
        .input("run two model tasks")
        .create_execution(
            mode="task_step",
            lineage={"task_id": "budget-task", "iteration_id": "iter-1", "step_id": "dag"},
            limits={"max_model_requests": 1},
        )
    )

    with pytest.raises(AgentExecutionLimitExceeded):
        await execution.async_get_data()

    meta = await execution.async_get_meta()
    assert meta["status"] == "blocked"
    assert meta["route_plan"]["selected_route"] == "dynamic_task"
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 1


@pytest.mark.asyncio
async def test_agent_execution_task_step_budget_covers_skills_model_stage(tmp_path):
    MockAgentExecutionRequester.requests = []
    skill_root = tmp_path / "skill-pack" / "skills" / "task-step"
    _write_skill(skill_root)
    agent = _create_agent("task-step-skills-budget").use_skills(
        str(tmp_path / "skill-pack"),
        mode="required",
        auto_allow=True,
    )
    execution = (
        agent
        .input("use the task step skill")
        .create_execution(
            mode="task_step",
            lineage={"task_id": "budget-task", "iteration_id": "iter-1", "step_id": "skills"},
            limits={"max_model_requests": 0},
        )
    )

    with pytest.raises(AgentExecutionLimitExceeded):
        await execution.async_get_data()

    meta = await execution.async_get_meta()
    assert meta["status"] == "blocked"
    assert meta["route_plan"]["selected_route"] == "skills"
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 0


@pytest.mark.asyncio
async def test_agent_execution_options_forward_skills_effort(tmp_path):
    captured: dict[str, Any] = {}
    skill_root = tmp_path / "skill-pack" / "skills" / "task-step"
    _write_skill(skill_root)
    agent = _create_agent("task-step-skills-options").use_skills(
        str(tmp_path / "skill-pack"),
        mode="required",
        auto_allow=True,
    )
    original_execute_skills_plan = agent.async_execute_skills_plan

    async def capture_execute_skills_plan(*args: Any, **kwargs: Any):
        captured["effort"] = kwargs.get("effort")
        return await original_execute_skills_plan(*args, **kwargs)

    agent.async_execute_skills_plan = capture_execute_skills_plan
    execution = (
        agent
        .input("use the task step skill")
        .create_execution(
            options=ExecutionOptions.model_validate(
                {"routes": {"skills": SkillsRouteOptions(effort="fast")}}
            ),
            limits={"max_model_requests": 0},
        )
    )

    with pytest.raises(AgentExecutionLimitExceeded):
        await execution.async_get_data()

    meta = await execution.async_get_meta()
    assert captured["effort"] == "fast"
    assert meta["options"]["routes"]["skills"]["effort"] == "fast"
    assert meta["effective_options"]["execution"]["mode"] == "one_turn"
    assert meta["effective_options"]["execution"]["limits"]["max_model_requests"] == 0
    assert meta["consumed_options"]["routes.skills.effort"] == {
        "value": "fast",
        "owner": "AgentlySkillsExecutor",
    }


@pytest.mark.asyncio
async def test_two_task_step_executions_can_be_correlated_as_developer_loop():
    agent = _create_agent("task-step-loop")
    first = (
        agent
        .input("first step")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            mode="task_step",
            lineage={"task_id": "loop-task", "iteration_id": "iter-1", "step_id": "first"},
            limits={"max_model_requests": 1},
        )
    )
    first_data = await first.async_get_data()
    first_meta = await first.async_get_meta()

    second = (
        agent
        .input({"previous": first_data})
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            mode="task_step",
            lineage={
                "task_id": "loop-task",
                "iteration_id": "iter-2",
                "step_id": "second",
                "parent_execution_id": first_meta["execution_id"],
            },
            limits={"max_model_requests": 1},
        )
    )

    second_stream = [item async for item in second.get_async_generator(type="instant") if item.is_complete]
    second_meta = await second.async_get_meta()

    assert second_meta["lineage"]["parent_execution_id"] == first_meta["execution_id"]
    assert second_meta["lineage"]["iteration_id"] == "iter-2"
    assert all(item.meta["execution_id"] == second_meta["execution_id"] for item in second_stream if item.meta)
    assert all(item.meta["lineage"]["task_id"] == "loop-task" for item in second_stream if item.meta)


@pytest.mark.asyncio
async def test_agent_execution_records_workspace_refs_from_bound_agent_workspace(tmp_path):
    agent = _create_agent("task-step-workspace-binding").use_workspace(tmp_path / "run")
    execution = (
        agent
        .input("workspace-bound step")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            mode="task_step",
            lineage={
                "task_id": "workspace-task",
                "iteration_id": "iter-1",
                "step_id": "record",
                "scope": {"area": "agent-execution"},
            },
            limits={"max_model_requests": 1},
        )
    )

    data = await execution.async_get_data()
    workspace_record = await execution.async_record_workspace(
        content={"answer": data["answer"]},
        summary="workspace-bound task-step record",
        checkpoint=True,
    )
    meta = await execution.async_get_meta()

    assert workspace_record["record"]["collection"] == "observations"
    assert workspace_record["record"]["scope"]["task_id"] == "workspace-task"
    assert workspace_record["record"]["scope"]["area"] == "agent-execution"
    assert workspace_record["record"]["source"]["type"] == "agent_execution"
    assert workspace_record["checkpoint"] is not None
    assert meta["workspace_refs"]["observations"] == [workspace_record["record"]["id"]]
    assert meta["workspace_refs"]["checkpoints"] == [workspace_record["checkpoint"]["id"]]
    assert agent.workspace is not None
    assert await agent.workspace.get_data(workspace_record["record"]) == {"answer": data["answer"]}
    history = await agent.workspace.checkpoint_history("workspace-task", step_id="record")
    assert [item["id"] for item in history] == [workspace_record["checkpoint"]["id"]]


@pytest.mark.asyncio
async def test_agent_execution_workspace_record_requires_bound_workspace():
    agent = _create_agent("task-step-workspace-missing")
    execution = (
        agent
        .input("missing workspace")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(mode="task_step", limits={"max_model_requests": 1})
    )

    with pytest.raises(RuntimeError, match="agent.use_workspace"):
        await execution.async_record_workspace()


def test_agent_execution_record_workspace_sync_wrapper_uses_function_shifter(tmp_path):
    agent = _create_agent("task-step-workspace-sync").use_workspace(tmp_path / "run")
    execution = (
        agent
        .input("workspace-bound sync step")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            mode="task_step",
            lineage={"task_id": "workspace-sync-task", "step_id": "record-sync"},
            limits={"max_model_requests": 1},
        )
    )

    workspace_record = execution.record_workspace(content={"sync": True}, checkpoint=True)
    meta = execution.get_meta()

    assert workspace_record["record"]["collection"] == "observations"
    assert workspace_record["checkpoint"] is not None
    assert meta["workspace_refs"]["observations"] == [workspace_record["record"]["id"]]
    assert meta["workspace_refs"]["checkpoints"] == [workspace_record["checkpoint"]["id"]]
