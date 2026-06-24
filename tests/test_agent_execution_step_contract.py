from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.core import PluginManager
from agently.core.application.AgentExecution import AgentExecutionLimitExceeded, AgentExecutionResult
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


class MockScopedActionRequester(MockAgentExecutionRequester):
    name = "MockScopedActionRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "next_action" in text and "execution_commands" in text:
            action_id = "blocked_action" if "blocked_action" in text else "allowed_action"
            payload = {
                "next_action": "execute",
                "execution_commands": [
                    {
                        "purpose": f"Run {action_id}",
                        "action_id": action_id,
                        "action_input": {},
                    }
                ],
            }
        elif "[ACTION RESULTS]" in text:
            payload = {"answer": "used scoped action", "status": "ready"}
        else:
            payload = {"answer": "plain-text-delta", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockAgentExecutionIsolationRequester(MockAgentExecutionRequester):
    name = "MockAgentExecutionIsolationRequester"
    active_requests = 0
    max_active_requests = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockAgentExecutionIsolationRequester.active_requests = 0
        MockAgentExecutionIsolationRequester.max_active_requests = 0

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={
                "input": self.prompt.get("input"),
                "system": self.prompt.get("system"),
                "output": self.prompt.get("output"),
            },
            request_options={"stream": True},
            request_url="mock://agent-execution-isolation",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        MockAgentExecutionIsolationRequester.active_requests += 1
        MockAgentExecutionIsolationRequester.max_active_requests = max(
            MockAgentExecutionIsolationRequester.max_active_requests,
            MockAgentExecutionIsolationRequester.active_requests,
        )
        try:
            await asyncio.sleep(0.1)
            payload = DataFormatter.sanitize(request_data.data)
            prompt_input = payload.get("input")
            persistent_system = payload.get("system")
            MockAgentExecutionRequester.requests.append(json.dumps(payload, ensure_ascii=False))
            yield "message", json.dumps(
                {
                    "answer": prompt_input,
                    "system": persistent_system,
                },
                ensure_ascii=False,
            )
        finally:
            MockAgentExecutionIsolationRequester.active_requests -= 1


def _create_agent(name: str = "agent-execution-step-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockAgentExecutionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_execution_isolation_agent(name: str = "agent-execution-isolation-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockAgentExecutionIsolationRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_action_agent(name: str = "agent-execution-action-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockAgentExecutionActionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_scoped_action_agent(name: str = "agent-execution-scoped-action-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockScopedActionRequester, activate=True)
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


def _install_site_skill(tmp_path: Path) -> Path:
    skill_root = tmp_path / "skill-pack" / "skills" / "website-builder"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        """---
name: Website Builder
description: Use to plan and verify small website deliverables.
---

# Website Builder

Help build a small product website from supplied facts.
""",
        encoding="utf-8",
    )
    return tmp_path / "skill-pack"


class MockGoalPursuitRequester(MockAgentExecutionRequester):
    """Model-driven mock for the goal-pursuit (AgentTask) execution path.

    Drives plan -> bounded step -> verify through real model output rather than
    patching production AgentTask methods, so the execution->AgentTask wiring is
    exercised end to end.
    """

    name = "MockGoalPursuitRequester"
    final_result = "accepted result"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "direct",
                "step_instruction": "run one bounded step",
                "expected_evidence": "final output evidence",
                "rationale": "one bounded step is enough",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": MockGoalPursuitRequester.final_result,
                "evidence": ["evidence recorded"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "model verifier accepted the evidence",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": MockGoalPursuitRequester.final_result,
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatReplanRequester(MockAgentExecutionRequester):
    name = "MockFlatReplanRequester"
    verify_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockFlatReplanRequester.verify_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "dynamic_task",
                "step_instruction": "collect one piece of evidence",
                "expected_evidence": "evidence exists",
                "rationale": "the mock intentionally asks for DAG to prove flat host policy wins",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "flat bounded step result",
                "evidence": ["flat evidence"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            MockFlatReplanRequester.verify_calls += 1
            if MockFlatReplanRequester.verify_calls == 1:
                payload = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "first pass lacks enough evidence",
                    "missing_criteria": ["needs one more bounded pass"],
                    "replan_instruction": "Run one more flat bounded step.",
                    "final_result_required": True,
                    "final_result": "",
                }
            else:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "second pass is enough",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": True,
                    "final_result": "flat accepted result",
                }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatActionRequester(MockAgentExecutionRequester):
    name = "MockFlatActionRequester"
    action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockFlatActionRequester.action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "actions",
                "step_instruction": "Call the probe_action framework Action.",
                "expected_evidence": "probe_action result",
                "rationale": "The task needs action evidence.",
            }
        elif "next_action" in text and "execution_commands" in text:
            MockFlatActionRequester.action_planning_calls += 1
            if MockFlatActionRequester.action_planning_calls == 1:
                payload = {
                    "next_action": "execute",
                    "execution_commands": [
                        {
                            "purpose": "Collect probe action evidence.",
                            "action_id": "probe_action",
                            "action_input": {},
                        }
                    ],
                }
            else:
                payload = {"next_action": "response", "execution_commands": []}
        elif "[ACTION RESULTS]" in text:
            payload = {
                "step_result": "action evidence collected",
                "evidence": ["probe_action executed"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "action evidence is present",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "flat action accepted result",
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan a TaskBoard for this submitted task" in text:
            payload = {
                "board_goal": "Complete the task through a board.",
                "cards": [
                    {
                        "id": "collect",
                        "action_block": "Collect and summarize evidence.",
                        "objective": "Collect one fact and summarize it.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The fact is summarized.",
                        "allowed_execution_shape": "model",
                    }
                ],
                "reflection_points": ["Check the collected fact before finalizing."],
                "completion_gate": "All cards completed and final answer synthesized.",
                "why_this_effort_shape": "One card is enough for this mock task.",
                "risk_notes": [],
            }
        elif "Execute exactly one TaskBoard card" in text:
            payload = {
                "status": "completed",
                "answer": "taskboard card result",
                "evidence": ["taskboard card evidence"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif "Synthesize the final result for this TaskBoard task" in text:
            payload = {
                "accepted": True,
                "reason": "completed card evidence satisfies the criterion",
                "final_result": "taskboard accepted result",
                "missing_criteria": [],
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardReadbackRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardReadbackRequester"
    last_action_id = ""
    readback_planning_seen = False
    review_planning_prompt = ""

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockTaskBoardReadbackRequester.last_action_id = ""
        MockTaskBoardReadbackRequester.readback_planning_seen = False
        MockTaskBoardReadbackRequester.review_planning_prompt = ""

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan a TaskBoard for this submitted task" in text:
            payload = {
                "board_goal": "Complete the task through evidence readback.",
                "cards": [
                    {
                        "id": "collect",
                        "action_block": "Collect opaque evidence.",
                        "objective": "Collect one opaque evidence artifact.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The opaque evidence is available as a cold artifact ref.",
                        "allowed_execution_shape": "model",
                    },
                    {
                        "id": "review",
                        "action_block": "Review evidence.",
                        "objective": "Review evidence by reading cold artifact refs when previews are insufficient.",
                        "depends_on": ["collect"],
                        "evidence_to_use": ["collect"],
                        "done_when": "The cold artifact ref has been read back.",
                        "allowed_execution_shape": "model",
                    },
                ],
                "reflection_points": ["Read cold refs when dependency previews are insufficient."],
                "completion_gate": "Both cards completed and final answer synthesized.",
                "why_this_effort_shape": "The second card depends on the first card evidence.",
                "risk_notes": [],
            }
        elif "next_action" in text and "execution_commands" in text:
            artifact_id_match = re.search(r"act_art_[0-9a-f]+", text)
            action_call_id_match = re.search(r"act_call_[0-9a-f]+", text)
            if "read_action_artifact" in text and artifact_id_match is not None:
                MockTaskBoardReadbackRequester.review_planning_prompt = text
                MockTaskBoardReadbackRequester.readback_planning_seen = True
                MockTaskBoardReadbackRequester.last_action_id = "read_action_artifact"
                payload = {
                    "next_action": "execute",
                    "execution_commands": [
                        {
                            "purpose": "Read dependency cold artifact.",
                            "action_id": "read_action_artifact",
                            "action_input": {
                                "artifact_id": artifact_id_match.group(0) if artifact_id_match else "missing",
                                "action_call_id": action_call_id_match.group(0) if action_call_id_match else "",
                            },
                        }
                    ],
                }
            else:
                MockTaskBoardReadbackRequester.last_action_id = "produce_large_evidence"
                payload = {
                    "next_action": "execute",
                    "execution_commands": [
                        {
                            "purpose": "Produce an opaque artifact.",
                            "action_id": "produce_large_evidence",
                            "action_input": {},
                        }
                    ],
                }
        elif "[ACTION RESULTS]" in text:
            if MockTaskBoardReadbackRequester.last_action_id == "read_action_artifact":
                payload = {
                    "status": "completed",
                    "answer": "readback confirmed",
                    "evidence": ["read_action_artifact returned the dependency artifact"],
                    "remaining_work": [],
                    "diagnostics": [],
                }
            else:
                payload = {
                    "status": "completed",
                    "answer": "cold artifact produced",
                    "evidence": ["produce_large_evidence produced a cold artifact ref"],
                    "remaining_work": [],
                    "diagnostics": [],
                }
        elif "Execute exactly one TaskBoard card" in text:
            payload = {
                "status": "completed",
                "answer": "card completed without action",
                "evidence": ["card evidence"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif "Synthesize the final result for this TaskBoard task" in text:
            payload = {
                "accepted": True,
                "reason": "dependency evidence was read back",
                "final_result": "taskboard readback accepted result",
                "missing_criteria": [],
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


def _create_goal_pursuit_agent(name: str = "agent-execution-goal-pursuit"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockGoalPursuitRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_replan_agent(name: str = "agent-execution-flat-replan"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatReplanRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_action_agent(name: str = "agent-execution-flat-action"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatActionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_agent(name: str = "agent-execution-taskboard"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_readback_agent(name: str = "agent-execution-taskboard-readback"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardReadbackRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def test_execution_options_validate_known_route_schema():
    with pytest.raises(ValueError):
        ExecutionOptions.model_validate({"routes": {"skills": {"unknown": True}}})


def test_create_task_execution_parameter_normalizes_and_rejects(tmp_path):
    agent = _create_agent("execution-strategy-normalization").use_workspace(tmp_path / "workspace")

    execution = agent.create_task(
        goal="Do the task.",
        success_criteria=["The task is done."],
        execution="flat_react",
    )
    assert execution.task_options["execution"] == "flat"

    taskboard_execution = agent.create_task(
        goal="Do the board task.",
        success_criteria=["The task is done."],
        execution="task_board",
    )
    assert taskboard_execution.task_options["execution"] == "taskboard"

    with pytest.raises(ValueError, match="execution must be one of"):
        agent.create_task(
            goal="Do the task.",
            success_criteria=["The task is done."],
            execution="unknown",
        )


@pytest.mark.asyncio
async def test_flat_execution_strategy_forces_linear_steps_and_keeps_replan_gate(tmp_path):
    agent = _create_flat_replan_agent("execution-flat-strategy").use_workspace(tmp_path / "workspace")

    execution = agent.create_task(
        goal="Produce a checked answer.",
        success_criteria=["The answer is accepted by verifier."],
        execution="flat",
        max_iterations=2,
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "flat accepted result"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["task_refs"]["execution_strategy"] == "flat"
    assert task_meta["execution_strategy"] == "flat"
    assert len(task_meta["iterations"]) == 2
    assert task_meta["iterations"][0]["plan"]["execution_shape"] == "dynamic_task"
    assert task_meta["iterations"][0]["plan"]["effective_execution_shape"] == "direct"
    assert task_meta["iterations"][0]["plan"]["step_execution"]["policy"]["execution_strategy"] == "flat"
    assert task_meta["iterations"][0]["plan"]["step_execution"]["policy"]["allow_dag_steps"] is False
    assert task_meta["iterations"][0]["verification"]["is_complete"] is False
    assert task_meta["iterations"][1]["verification"]["is_complete"] is True


@pytest.mark.asyncio
async def test_flat_actions_shape_activates_framework_actions_from_capabilities(tmp_path):
    agent = _create_flat_action_agent("execution-flat-action-strategy").use_workspace(tmp_path / "workspace")

    @agent.action_func
    def probe_action() -> dict[str, str]:
        return {"status": "ok", "evidence": "framework action executed"}

    execution = (
        agent.create_task(
            goal="Collect action evidence.",
            success_criteria=["The probe action executes."],
            execution="flat",
            max_iterations=1,
        )
        .use_actions(["probe_action"])
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    iteration = task_meta["iterations"][0]
    step_execution = iteration["plan"]["step_execution"]
    action_logs = iteration["execution_meta"]["logs"]["action_logs"]
    if isinstance(action_logs, dict):
        action_ids = list(action_logs.keys())
    else:
        action_ids = [item.get("action_id") for item in action_logs]

    assert result["accepted"] is True
    assert step_execution["effective_shape"] == "actions"
    assert step_execution["action_scope_source"] == "planner_capabilities"
    assert set(action_ids) == {"probe_action"}
    assert action_ids


@pytest.mark.asyncio
async def test_taskboard_execution_strategy_runs_framework_owned_board(tmp_path):
    agent = _create_taskboard_agent("execution-taskboard-strategy").use_workspace(tmp_path / "workspace")

    execution = agent.create_task(
        goal="Produce a board-managed answer.",
        success_criteria=["The board final answer is accepted."],
        execution="taskboard",
        max_iterations=2,
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = task_meta["result"]["taskboard"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["execution_strategy"] == "taskboard"
    assert result["final_result"] == "taskboard accepted result"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["task_refs"]["execution_strategy"] == "taskboard"
    assert task_meta["execution_strategy"] == "taskboard"
    assert taskboard["revision"]["card_results"]["collect"]["status"] == "completed"
    assert taskboard["evidence_view"]["cards"][0]["card_id"] == "collect"
    assert "content" not in taskboard["evidence_view"]["cards"][0]["artifact_refs"]


@pytest.mark.asyncio
async def test_taskboard_card_can_read_dependency_action_artifact_refs(tmp_path):
    agent = _create_taskboard_readback_agent("execution-taskboard-readback").use_workspace(tmp_path / "workspace")

    @agent.action_func
    def produce_large_evidence() -> dict[str, Any]:
        return {
            "records": [
                {
                    "title": "Hidden evidence",
                    "url": "https://example.test/evidence",
                    "snippet": "detail available only through artifact readback",
                }
            ],
            "padding": "x" * 9000,
        }

    execution = (
        agent.create_task(
            goal="Use a dependency cold artifact to finish the board.",
            success_criteria=["The review card reads back dependency evidence."],
            execution="taskboard",
            max_iterations=3,
        )
        .use_actions(["produce_large_evidence"])
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = task_meta["result"]["taskboard"]
    review_result = taskboard["revision"]["card_results"]["review"]
    review_actions = review_result["diagnostics"][-1]["evidence_summary"]["action_ids"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "taskboard readback accepted result"
    assert MockTaskBoardReadbackRequester.readback_planning_seen is True
    assert "read_action_artifact" in MockTaskBoardReadbackRequester.review_planning_prompt
    assert "read_action_artifact" in review_actions
    assert review_result["status"] == "completed"


@pytest.mark.asyncio
async def test_execution_first_chain_from_goal_accepts_skills_input_and_stream(tmp_path):
    skill_pack = _install_site_skill(tmp_path)
    agent = _create_goal_pursuit_agent("execution-first-goal-chain").use_workspace(tmp_path / "workspace")

    execution = (
        agent
        .goal("Build the site.", success_criteria=["The runnable page exists."])
        .use_skills(str(skill_pack), auto_allow=True)
        .input("Use the supplied product facts.")
        .effort("low")
    )

    stream_items = [item async for item in execution.get_async_generator()]
    meta = await execution.async_get_meta()

    assert type(execution).__name__ == "AgentExecution"
    assert execution.goal_items == ["Build the site."]
    assert execution.success_criteria_items == ["The runnable page exists."]
    assert execution.prompt_snapshot["input"] == "Use the supplied product facts."
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["effective_options"]["effort_strategy"]["max_iterations"] == 1
    assert any(item.path == "agent_task.phase.configured" for item in stream_items)
    assert any(item.path == "agent_task.phase.terminal" for item in stream_items)


def test_goal_alias_and_detailed_effort_strategy_are_normalized(tmp_path):
    agent = _create_agent("execution-goals-effort-alias").use_workspace(tmp_path / "workspace")

    execution = (
        agent
        .goals(
            ["Build the site.", "Publish a launch checklist."],
            success_criteria=["The runnable page exists."],
        )
        .effort(
            "high",
            budget={
                "iteration_limit": 4,
                "model_call_limit": 8,
                "wall_time_seconds": 90,
                "no_progress_seconds": 30,
            },
            planning={"depth": "expanded", "max_plan_items": 8},
            verification={"strictness": "strict"},
            replan={"policy": "on_verification_failure", "limit": 2},
            progress={"detail": "phase"},
        )
    )

    effort_strategy = execution.effective_options["effort_strategy"]
    assert execution.goal_items == ["Build the site.", "Publish a launch checklist."]
    assert execution.success_criteria_items == ["The runnable page exists."]
    assert effort_strategy["name"] == "high"
    assert effort_strategy["max_iterations"] == 4
    assert effort_strategy["planning_depth"] == "expanded"
    assert effort_strategy["verifier_strength"] == "strict"
    assert effort_strategy["planning"]["max_plan_items"] == 8
    assert effort_strategy["replan"]["limit"] == 2
    assert effort_strategy["progress"]["detail"] == "phase"
    assert execution.limits["max_model_requests"] == 8
    assert execution.limits["max_seconds"] == 90.0
    assert execution.limits["max_no_progress_seconds"] == 30.0
    assert execution.effective_options["execution"]["limits"]["max_model_requests"] == 8

    explicit_limits = (
        agent
        .create_execution(limits={"max_model_requests": 2})
        .effort("high", budget={"model_call_limit": 8})
    )
    assert explicit_limits.limits["max_model_requests"] == 2

    mutable_effort_limits = agent.create_execution().effort("medium", budget={"model_call_limit": 4})
    mutable_effort_limits.effort("high", budget={"model_call_limit": 6})
    assert mutable_effort_limits.limits["max_model_requests"] == 6
    mutable_effort_limits.effort("low")
    assert mutable_effort_limits.limits["max_model_requests"] is None


@pytest.mark.asyncio
async def test_goal_pursuit_uses_detailed_effort_iteration_limit(tmp_path):
    agent = _create_goal_pursuit_agent("execution-detailed-effort-task").use_workspace(tmp_path / "workspace")
    execution = (
        agent
        .goal("Build the site.", success_criteria=["The runnable page exists."])
        .effort("low", budget={"iteration_limit": 2})
    )

    await execution.async_start()
    meta = await execution.async_get_meta()

    assert meta["logs"]["route_logs"]["agent_task"]["max_iterations"] == 2
    assert meta["consumed_options"]["effort.max_iterations"] == {
        "value": 2,
        "owner": "AgentTaskLoop",
    }


@pytest.mark.asyncio
async def test_goal_pursuit_wall_clock_budget_is_owned_by_agent_task_loop(tmp_path):
    agent = _create_goal_pursuit_agent("execution-task-route-deadline-owner").use_workspace(tmp_path / "workspace")
    execution = (
        agent
        .goal("Build the site.", success_criteria=["The runnable page exists."])
        .create_execution(limits={"max_seconds": 0.2, "max_no_progress_seconds": 5})
    )

    async def slow_request_plan(_iteration_index, _context_pack):
        await asyncio.sleep(0.6)
        return {
            "step_instruction": "build the site",
            "expected_evidence": "site exists",
            "rationale": "this should be interrupted by the AgentTaskLoop deadline",
        }

    cast(Any, execution)._agent_task_step_overrides = {"_request_plan": slow_request_plan}

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert result["status"] == "timed_out"
    assert execution.status == "timed_out"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["close_snapshot"]["task"]["status"] == "timed_out"
    assert "plan stage" in result["reason"]


def test_execution_first_chain_allows_capabilities_before_goal(tmp_path):
    skill_pack = _install_site_skill(tmp_path)
    agent = _create_agent("execution-first-skill-first").use_workspace(tmp_path / "workspace")

    execution = (
        agent
        .use_skills(str(skill_pack), auto_allow=True)
        .goal("Build the site.", success_criteria=["The runnable page exists."])
        .input("Use the supplied product facts.")
    )

    assert type(execution).__name__ == "AgentExecution"
    assert execution.goal_items == ["Build the site."]
    assert execution.success_criteria_items == ["The runnable page exists."]
    assert execution.prompt_snapshot["input"] == "Use the supplied product facts."
    assert agent.request.prompt.get(inherit=False) == {}
    assert agent._collect_skill_selectors(skills=None, mode="model_decision") == []
    assert execution.local_skill_selectors


def test_goal_accepts_multiple_goals_and_optional_success_criteria():
    agent = _create_agent("execution-first-multiple-goals")

    execution = agent.goal(
        ["Build the site.", "Include pricing and contact sections."],
        ["The final site includes both sections."],
    )

    assert type(execution).__name__ == "AgentExecution"
    assert execution.goal_items == ["Build the site.", "Include pricing and contact sections."]
    assert execution.success_criteria_items == ["The final site includes both sections."]


@pytest.mark.asyncio
async def test_execution_first_chain_allows_goal_after_prompt_output(tmp_path):
    agent = _create_goal_pursuit_agent("execution-first-goal-after-prompt").use_workspace(tmp_path / "workspace")
    execution = (
        agent
        .input("Use these facts.")
        .output({"summary": (str, "summary", True)}, format="json")
        .goal("Write the final summary.", success_criteria=["The final summary is returned."])
    )

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["accepted"] is True
    assert meta["route"]["selected_route"] == "agent_task"
    assert execution.prompt_snapshot["input"] == "Use these facts."
    assert execution.prompt_snapshot["output"]["summary"][0] is str
    assert execution.goal_items == ["Write the final summary."]


@pytest.mark.asyncio
async def test_allow_create_task_false_blocks_goal_pursuit(tmp_path):
    """ISSUE-007: allow_create_task=False is an enforced limit, not a no-op."""
    agent = _create_goal_pursuit_agent("execution-no-create-task").use_workspace(tmp_path / "workspace")
    execution = (
        agent
        .goal("Build the site.", success_criteria=["The runnable page exists."])
        .create_execution(limits={"allow_create_task": False})
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert result["status"] == "blocked"
    assert result["accepted"] is False
    assert meta["route"]["selected_route"] == "agent_task"


@pytest.mark.asyncio
async def test_route_policy_block_and_deterministic_fallback():
    """ISSUE-017: on_violation='block' surfaces a blocked route; fallback is deterministic."""
    from types import SimpleNamespace
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.routing import (
        HybridRoutePlanner,
    )

    blocked_exec = SimpleNamespace(
        options={"route_policy": {"allowed_routes": ["skills"], "on_violation": "block"}},
        effective_options={},
        local_dynamic_task_candidates=[],
        local_skill_selectors=[],
        local_skills_pack_selectors=[],
        local_action_ids=[],
    )
    planner = HybridRoutePlanner(cast(Any, None), execution=blocked_exec)
    assert planner.allowed_routes() == {"skills"}
    assert planner.route_allowed("model_request") is False
    assert planner.on_violation() == "block"
    route, meta = await planner.select_route()
    assert route == "route_policy_blocked"
    assert meta["selected_by"] == "route_policy_violation"

    # Default on_violation is a fallback to model_request (backward compatible).
    fallback_exec = SimpleNamespace(
        options={"route_policy": {"allowed_routes": ["skills"]}},
        effective_options={},
        local_dynamic_task_candidates=[],
        local_skill_selectors=[],
        local_skills_pack_selectors=[],
        local_action_ids=[],
    )
    fallback_planner = HybridRoutePlanner(cast(Any, None), execution=fallback_exec)
    route, meta = await fallback_planner.select_route()
    assert route == "model_request"
    assert meta["selected_by"] == "route_policy_fallback"


def test_agent_execution_context_enforces_nesting_budget():
    """ISSUE-007: max_nested_agent_steps is an enforced recursion control."""
    from agently.core.application.AgentExecution import AgentExecutionContext, AgentExecutionLimitExceeded

    root = AgentExecutionContext(
        execution_id="root",
        lineage={},
        limits={"max_nested_agent_steps": 1},
        nesting_depth=0,
        nesting_budget=1,
    )
    root.raise_if_nesting_exceeded()  # depth 0 within budget 1

    nested_ok = AgentExecutionContext(
        execution_id="nested-1", lineage={}, limits={}, nesting_depth=1, nesting_budget=1
    )
    nested_ok.raise_if_nesting_exceeded()  # depth 1 within budget 1

    nested_over = AgentExecutionContext(
        execution_id="nested-2", lineage={}, limits={}, nesting_depth=2, nesting_budget=1
    )
    with pytest.raises(AgentExecutionLimitExceeded) as raised:
        nested_over.raise_if_nesting_exceeded()
    assert raised.value.limit_name == "max_nested_agent_steps"


@pytest.mark.asyncio
async def test_agent_quick_prompt_returns_execution_and_result_facade():
    MockAgentExecutionRequester.requests = []
    agent = _create_agent("quick-prompt-execution-result")

    execution = agent.input("quick prompt").output({"answer": (str, "answer", True)}, format="json")
    result = execution.get_result()
    data = await result.async_get_data()
    meta = await result.async_get_meta()

    assert type(execution).__name__ == "AgentExecution"
    assert isinstance(result, AgentExecutionResult)
    assert data["answer"] == "ok-1"
    assert meta["execution_id"] == execution.id
    assert agent.request.prompt.get(inherit=False) == {}


def test_agent_define_parameter_and_builder_forms_write_definition_state():
    agent = _create_agent("agent-define-contract")

    builder = agent.define(
        model="mock-model",
        prompt={"system": "Base policy"},
        settings={"runtime.session_id": "define-session"},
        policy={"handler": "default"},
    )
    assert builder.info({"tenant": "demo"}) is builder

    prompt_snapshot = agent.agent_prompt.get(inherit=False)
    assert isinstance(prompt_snapshot, dict)
    assert prompt_snapshot["system"] == "Base policy"
    assert prompt_snapshot["info"] == {"tenant": "demo"}
    assert agent.request.prompt.get(inherit=False) == {}
    assert agent.settings.get("runtime.session_id") == "define-session"
    assert agent.settings.get("policy_approval.handler") == "default"

    builder_agent = _create_agent("agent-define-builder-contract")
    builder_2 = builder_agent.define()
    assert builder_2.role("Support assistant") is builder_2
    assert builder_agent.agent_prompt.get("system.your_role", inherit=False) == "Support assistant"
    assert builder_agent.request.prompt.get(inherit=False) == {}


def test_create_task_and_task_loop_return_strategy_execution_drafts(tmp_path):
    agent = _create_agent("task-strategy-drafts")

    task_execution = agent.create_task(
        task_id="task-draft",
        goal="Draft a task",
        success_criteria=["The task is drafted."],
        workspace=tmp_path / "task",
    )
    loop_execution = agent.create_task_loop(
        task_id="loop-draft",
        goal="Loop a task",
        success_criteria=["The loop is drafted."],
        workspace=tmp_path / "loop",
    )

    assert type(task_execution).__name__ == "AgentExecution"
    assert task_execution.strategy_name == "task"
    assert task_execution.task_options["task_id"] == "task-draft"
    assert type(loop_execution).__name__ == "AgentExecution"
    assert loop_execution.strategy_name == "task_loop"


def test_removed_transitional_surfaces_are_not_available():
    agent = _create_agent("removed-transitional-surfaces")
    execution = agent.create_execution()

    assert not hasattr(agent, "create_turn")
    assert not hasattr(agent, "set_turn_prompt")
    assert not hasattr(agent, "set_request_prompt")
    assert not hasattr(agent, "remove_request_prompt")
    assert not hasattr(agent, "success_criteria")
    assert not hasattr(agent, "success_standards")
    assert hasattr(agent, "goals")
    assert hasattr(agent, "remove_execution_prompt")
    assert not hasattr(execution, "success_criteria")
    assert not hasattr(execution, "success_standards")
    assert hasattr(execution, "goals")
    assert not hasattr(execution, "remove_request_prompt")
    assert hasattr(execution, "remove_execution_prompt")


@pytest.mark.asyncio
async def test_same_agent_quick_prompt_executions_are_request_scoped():
    MockAgentExecutionRequester.requests = []
    agent = _create_execution_isolation_agent("same-agent-execution-isolation")
    agent.system("shared policy", always=True)

    results = await asyncio.gather(
        agent.input("request-A").output({"answer": (str, "answer", True)}, format="json").async_start(),
        agent.input("request-B").output({"answer": (str, "answer", True)}, format="json").async_start(),
        agent.input("request-C").output({"answer": (str, "answer", True)}, format="json").async_start(),
    )

    assert [result["answer"] for result in results] == ["request-A", "request-B", "request-C"]
    assert [result["system"] for result in results] == ["shared policy", "shared policy", "shared policy"]
    assert MockAgentExecutionIsolationRequester.max_active_requests == 3
    assert agent.request.prompt.get(inherit=False) == {}
    request_payloads = [json.loads(item) for item in MockAgentExecutionRequester.requests]
    assert [payload["input"] for payload in request_payloads] == ["request-A", "request-B", "request-C"]


@pytest.mark.asyncio
async def test_agent_execution_default_stream_meta_uses_execution_id_and_lineage():
    MockAgentExecutionRequester.requests = []
    agent = _create_agent("default-execution-stream")
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
    assert meta["limits"]["max_model_requests"] is None
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 1
    assert any(item.path == "route.selected" for item in stream_items)
    assert all(item.meta["execution_id"] == meta["execution_id"] for item in stream_items if item.meta)
    assert all("execution_mode" not in item.meta for item in stream_items if item.meta)


@pytest.mark.asyncio
async def test_agent_execution_rejects_removed_mode_argument():
    agent = _create_agent("removed-mode-argument")
    removed_mode_kwargs = {"mode": "removed"}

    with pytest.raises(TypeError):
        (
            agent
            .input("legacy mode argument")
            .output({"answer": (str, "answer", True)}, format="json")
            .create_execution(**removed_mode_kwargs)
        )


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
    assert action_items[0].meta is not None
    assert action_items[0].meta["lineage"]["task_id"] == "action-task"


@pytest.mark.asyncio
async def test_agent_execution_action_scope_filters_action_runtime_boundary():
    agent = _create_scoped_action_agent("action-scope-runtime-boundary")
    agent.set_action_loop(max_rounds=1)

    @agent.action_func
    def allowed_action() -> dict[str, str]:
        return {"status": "allowed"}

    @agent.action_func
    def blocked_action() -> dict[str, str]:
        return {"status": "blocked"}

    execution = (
        agent
        .create_execution()
        .use_actions(["allowed_action"])
        .input("use the scoped action")
        .output({"answer": (str, "answer", True)}, format="json")
    )

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()
    planning_calls = [
        call
        for call in MockAgentExecutionRequester.requests
        if "next_action" in call and "execution_commands" in call
    ]
    action_ids = [item.get("action_id") for item in meta["logs"]["action_logs"]]

    assert data["answer"] == "used scoped action"
    assert planning_calls
    assert "allowed_action" in planning_calls[0]
    assert "blocked_action" not in planning_calls[0]
    assert action_ids == ["allowed_action"]
    assert meta["diagnostics"]["action_scope"]["allowed_action_ids"] == ["allowed_action"]


@pytest.mark.asyncio
async def test_required_action_blocks_when_model_skips_required_evidence():
    agent = _create_action_agent("required-action-missing")

    @agent.action_func
    def required_lookup() -> dict[str, str]:
        return {"status": "looked-up"}

    execution = (
        agent
        .require_actions("required_lookup")
        .input("answer directly without using the required action")
        .output({"answer": (str, "answer", True)}, format="json")
    )

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["status"] == "blocked"
    assert data["accepted"] is False
    assert meta["status"] == "blocked"
    assert meta["route"]["selected_by"] == "required_capability"
    assert meta["effective_options"]["capability_constraints"]["actions"]["required"] == ["required_lookup"]
    required_diagnostics = meta["diagnostics"]["required_capabilities"][0]
    assert required_diagnostics["missing_actions"] == ["required_lookup"]


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
async def test_agent_execution_bounded_step_meta_lineage_and_limit_success():
    MockAgentExecutionRequester.requests = []
    agent = _create_agent("task-step-success")
    execution = (
        agent
        .input("produce one bounded answer")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            lineage={"task_id": "task-1", "iteration_id": "iter-1", "step_id": "draft"},
            limits={"max_model_requests": 1},
        )
    )

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["answer"] == "ok-1"
    assert meta["lineage"]["task_id"] == "task-1"
    assert meta["limits"]["max_model_requests"] == 1
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 1


@pytest.mark.asyncio
async def test_agent_execution_bounded_step_blocks_when_direct_model_budget_exceeded():
    agent = _create_agent("task-step-direct-budget")
    execution = (
        agent
        .input("this should exceed budget before provider call")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
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
async def test_agent_execution_bounded_step_budget_covers_dynamic_task_model_tasks():
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
async def test_agent_execution_bounded_step_budget_covers_skills_model_stage(tmp_path):
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
    agent = _create_agent("task-step-skills-options")
    original_execute_skills_plan = agent.async_execute_skills_plan

    async def capture_execute_skills_plan(*args: Any, **kwargs: Any):
        captured["effort"] = kwargs.get("effort")
        captured["output_format"] = kwargs.get("output_format")
        return await original_execute_skills_plan(*args, **kwargs)

    agent.async_execute_skills_plan = capture_execute_skills_plan
    execution = (
        agent
        .use_skills(str(tmp_path / "skill-pack"), mode="required", auto_allow=True)
        .input("use the task step skill")
        .create_execution(
            options=ExecutionOptions.model_validate(
                {"routes": {"skills": SkillsRouteOptions(effort="fast", output_format="flat_markdown")}}
            ),
            limits={"max_model_requests": 0},
        )
    )

    with pytest.raises(AgentExecutionLimitExceeded):
        await execution.async_get_data()

    meta = await execution.async_get_meta()
    assert captured["effort"] == "fast"
    assert captured["output_format"] == "flat_markdown"
    assert meta["options"]["routes"]["skills"]["effort"] == "fast"
    assert meta["options"]["routes"]["skills"]["output_format"] == "flat_markdown"
    assert meta["effective_options"]["execution"]["limits"]["max_model_requests"] == 0
    assert meta["consumed_options"]["routes.skills.effort"] == {
        "value": "fast",
        "owner": "AgentlySkillsExecutor",
    }
    assert meta["consumed_options"]["routes.skills.output_format"] == {
        "value": "flat_markdown",
        "owner": "AgentlySkillsExecutor",
    }


@pytest.mark.asyncio
async def test_two_bounded_step_executions_can_be_correlated_as_developer_loop():
    agent = _create_agent("task-step-loop")
    first = (
        agent
        .input("first step")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
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
    evidence_link_id = meta["workspace_refs"]["verification_evidence"][0]
    assert agent.workspace is not None
    assert await agent.workspace.get_data(workspace_record["record"]) == {"answer": data["answer"]}
    history = await agent.workspace.checkpoint_history("workspace-task", step_id="record")
    assert [item["id"] for item in history] == [workspace_record["checkpoint"]["id"]]
    evidence_links = await agent.workspace.links(workspace_record["record"], relation="checkpointed_by")
    assert [item["id"] for item in evidence_links] == [evidence_link_id]
    assert evidence_links[0]["target_id"] == workspace_record["checkpoint"]["id"]
    assert evidence_links[0]["meta"]["evidence"]["execution_id"] == meta["execution_id"]
    assert evidence_links[0]["meta"]["owner"] == "AgentExecution"


@pytest.mark.asyncio
async def test_agent_execution_workspace_record_uses_execution_scoped_lazy_default_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _create_agent("task-step-workspace-missing")
    workspace = agent.workspace
    assert getattr(workspace, "is_materialized") is False
    execution = (
        agent
        .input("missing workspace")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(limits={"max_model_requests": 1})
    )

    workspace_record = await execution.async_record_workspace()

    assert getattr(workspace, "is_materialized") is False
    assert getattr(execution.workspace, "is_materialized") is True
    assert workspace_record["record"]["collection"] == "observations"
    assert execution.workspace.root.exists()


def test_agent_execution_record_workspace_sync_wrapper_uses_function_shifter(tmp_path):
    agent = _create_agent("task-step-workspace-sync").use_workspace(tmp_path / "run")
    execution = (
        agent
        .input("workspace-bound sync step")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
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
    assert meta["workspace_refs"]["verification_evidence"]
