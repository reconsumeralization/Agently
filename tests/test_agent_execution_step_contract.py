from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agently import Agently
from agently.core import PluginManager, TaskBoardGraph, TaskBoardRevision, TaskBoardValidator
from agently.core.application.AgentExecution import AgentExecutionLimitExceeded, AgentExecutionResult
from agently.core.application.AgentTask import AgentTask
from agently.types.data import AgentlyRequestData
from agently.types.options import ExecutionOptions, SkillsRouteOptions
from agently.utils import DataFormatter
from agently.utils import Settings
from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.result_views import (
    get_async_generator as agent_execution_get_async_generator,
)


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


class _FakeStreamForGeneratorCancel:
    def __init__(self) -> None:
        self.items: list[Any] = []
        self.queues: list[asyncio.Queue[Any]] = []


class _FakeExecutionForGeneratorCancel:
    def __init__(self) -> None:
        self._completed = False
        self.stream = _FakeStreamForGeneratorCancel()

    async def async_start(self) -> None:
        await asyncio.sleep(0.01)
        for queue in list(self.stream.queues):
            await queue.put(None)
        raise RuntimeError("synthetic stream start failure")


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


@pytest.mark.asyncio
async def test_agent_execution_stream_cancel_retrieves_start_exception():
    event_loop = asyncio.get_running_loop()
    captured: list[dict[str, Any]] = []
    previous_handler = event_loop.get_exception_handler()
    event_loop.set_exception_handler(lambda _, context: captured.append(context))
    try:
        owner = _FakeExecutionForGeneratorCancel()
        generator = agent_execution_get_async_generator(cast(Any, owner))
        pending = asyncio.create_task(generator.__anext__())
        await asyncio.sleep(0)
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await asyncio.sleep(0.03)
    finally:
        event_loop.set_exception_handler(previous_handler)

    assert captured == []


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


def _taskboard_verification_payload(final_result: str = "taskboard accepted result") -> dict[str, Any]:
    return {
        "is_complete": True,
        "requires_block": False,
        "reason": "TaskBoard final evidence satisfies the success criteria.",
        "failure_analysis": "",
        "acceptance_delta": [],
        "missing_criteria": [],
        "repair_constraints": [],
        "next_step_requirements": [],
        "replan_instruction": "",
        "final_result_required": True,
        "final_result": final_result,
    }


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
            final_result = MockGoalPursuitRequester.final_result
            if "summary" in text:
                final_result = json.dumps({"summary": MockGoalPursuitRequester.final_result}, ensure_ascii=False)
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "model verifier accepted the evidence",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": final_result,
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


class MockFlatRepairConstraintRequester(MockAgentExecutionRequester):
    name = "MockFlatRepairConstraintRequester"
    plan_calls = 0
    verify_calls = 0
    second_plan_prompt = ""
    latest_step_instruction = ""

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockFlatRepairConstraintRequester.plan_calls = 0
        MockFlatRepairConstraintRequester.verify_calls = 0
        MockFlatRepairConstraintRequester.second_plan_prompt = ""
        MockFlatRepairConstraintRequester.latest_step_instruction = ""

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            MockFlatRepairConstraintRequester.plan_calls += 1
            if MockFlatRepairConstraintRequester.plan_calls == 1:
                step_instruction = "Draft a broad weekly report with all collected items."
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": step_instruction,
                    "expected_evidence": "Candidate report.",
                    "rationale": "Start with an initial report draft.",
                }
            else:
                MockFlatRepairConstraintRequester.second_plan_prompt = text
                if "repair_context" in text and "Reduce the report to 5-8 news items." in text:
                    step_instruction = (
                        "Revise the candidate report to contain 5-8 news items and keep existing grounded evidence."
                    )
                else:
                    step_instruction = "Repeat the broad weekly report draft without repair context."
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": step_instruction,
                    "expected_evidence": "Repaired report that satisfies the 5-8 item constraint.",
                    "rationale": "The latest verifier repair_context requires reducing the report to 5-8 items.",
                }
            MockFlatRepairConstraintRequester.latest_step_instruction = step_instruction
        elif "Execute exactly one bounded step" in text:
            if "Revise the candidate report to contain 5-8 news items" in text:
                payload = {
                    "step_result": "Repaired report produced.",
                    "candidate_final_result": "# Weekly\n\n" + "\n".join(f"- Item {index}" for index in range(1, 7)),
                    "evidence": ["repaired to 6 items"],
                    "remaining_work": [],
                }
            else:
                payload = {
                    "step_result": "Initial oversized report produced.",
                    "candidate_final_result": "# Weekly\n\n" + "\n".join(f"- Item {index}" for index in range(1, 16)),
                    "evidence": ["initial report has 15 items"],
                    "remaining_work": [],
                }
        elif "Verify the task against every success criterion" in text:
            MockFlatRepairConstraintRequester.verify_calls += 1
            if MockFlatRepairConstraintRequester.verify_calls == 1:
                payload = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "The draft includes 15 items but the task asks for 5-8.",
                    "failure_analysis": "The candidate artifact overshoots the accepted item count.",
                    "acceptance_delta": ["The report must include 5-8 news items, not 15."],
                    "missing_criteria": ["The report must include 5-8 news items, not 15."],
                    "repair_constraints": ["Reduce the report to 5-8 news items."],
                    "next_step_requirements": ["Revise the candidate report; do not restart evidence gathering."],
                    "replan_instruction": "Revise the report to satisfy the item-count constraint.",
                    "final_result_required": True,
                    "final_result": "",
                }
            else:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "The repaired report now has 6 items.",
                    "failure_analysis": "",
                    "acceptance_delta": [],
                    "missing_criteria": [],
                    "repair_constraints": [],
                    "next_step_requirements": [],
                    "replan_instruction": "",
                    "final_result_required": True,
                    "final_result": "repaired accepted result",
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


class MockFlatActionPlanningStallRequester(MockAgentExecutionRequester):
    name = "MockFlatActionPlanningStallRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "actions",
                "step_instruction": "Use the probe action.",
                "expected_evidence": "probe action result",
                "rationale": "The task needs action evidence.",
            }
        elif "next_action" in text and "execution_commands" in text:
            await asyncio.sleep(5)
            payload = {"next_action": "response", "execution_commands": []}
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": False,
                "requires_block": True,
                "reason": "execution timed out before action evidence was collected",
                "missing_criteria": ["Action planning timed out."],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "",
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatActionPlanningSlowRequester(MockAgentExecutionRequester):
    name = "MockFlatActionPlanningSlowRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "actions",
                "step_instruction": "Use the probe action.",
                "expected_evidence": "probe action result",
                "rationale": "The task needs action evidence.",
            }
        elif "next_action" in text and "execution_commands" in text:
            await asyncio.sleep(0.35)
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


class MockWorkspaceArtifactDraftStallRequester(MockAgentExecutionRequester):
    name = "MockWorkspaceArtifactDraftStallRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Write only the final Markdown artifact body for the AgentTask" in text:
            await asyncio.sleep(5)
            yield "message", "# Late artifact body"
            return
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "direct",
                "step_instruction": "Prepare a Workspace-backed final artifact.",
                "expected_evidence": "final.md is written and read back.",
                "rationale": "The task requests a deliverable file.",
                "deliverable_mode": "workspace_artifact",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "Control result ready; framework should draft the Workspace artifact.",
                "artifact_manifest": {"path": "final.md", "sections": [{"id": "summary", "title": "Summary"}]},
                "evidence": ["artifact source evidence"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": False,
                "requires_block": True,
                "reason": "The Workspace artifact was not delivered.",
                "missing_criteria": ["final.md readback is missing."],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "",
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatParallelActionRequester(MockAgentExecutionRequester):
    name = "MockFlatParallelActionRequester"
    action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockFlatParallelActionRequester.action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "actions",
                "step_instruction": "Collect independent evidence from two framework Actions.",
                "expected_evidence": "Both independent action results are present.",
                "rationale": "The actions are independent and can run in the same bounded step.",
            }
        elif "next_action" in text and "execution_commands" in text:
            MockFlatParallelActionRequester.action_planning_calls += 1
            if MockFlatParallelActionRequester.action_planning_calls == 1:
                payload = {
                    "next_action": "execute",
                    "execution_commands": [
                        {
                            "purpose": "Collect evidence A.",
                            "action_id": "slow_a",
                            "action_input": {},
                        },
                        {
                            "purpose": "Collect evidence B.",
                            "action_id": "slow_b",
                            "action_input": {},
                        },
                    ],
                }
            else:
                payload = {"next_action": "response", "execution_commands": []}
        elif "[ACTION RESULTS]" in text:
            payload = {
                "step_result": "parallel action evidence collected",
                "evidence": ["slow_a executed", "slow_b executed"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "both independent action results are present",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "flat parallel action accepted result",
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatEvidenceCandidateRequester(MockAgentExecutionRequester):
    name = "MockFlatEvidenceCandidateRequester"
    report = "# Weekly Report\n\n" + ("Detailed section with grounded evidence.\n" * 80)

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "direct",
                "step_instruction": "Produce the requested report.",
                "expected_evidence": "Complete report text and supporting evidence.",
                "rationale": "One bounded step can produce the report.",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "Report written; see evidence for the full Markdown.",
                "evidence": [self.report],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": "candidate_final_result" in text and "Weekly Report" in text,
                "requires_block": False,
                "reason": "candidate final report is visible to verifier",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "",
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
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("taskboard accepted result")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardControlRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardControlRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan a TaskBoard for this submitted task" in text:
            payload = {
                "board_goal": "Complete the task through a control card.",
                "cards": [
                    {
                        "id": "synthesize",
                        "action_block": "Synthesize and verify the final deliverable.",
                        "objective": "Produce the complete final Markdown deliverable and decide whether it is enough.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The final deliverable is complete and sufficient.",
                        "allowed_execution_shape": "control",
                    }
                ],
                "reflection_points": ["Check sufficiency in the control-card output."],
                "completion_gate": "The control card returns a complete accepted deliverable.",
                "why_this_effort_shape": "One control card can synthesize and self-check this task.",
                "risk_notes": [],
            }
        elif "Execute one TaskBoard control card with a single structured model request" in text:
            payload = {
                "status": "completed",
                "answer": "control card synthesized the final deliverable",
                "artifact_markdown": "# Control Result\n\nComplete deliverable body.",
                "sufficient": True,
                "next_board_action": "finalize",
                "gaps": [],
                "evidence": ["control evidence summary"],
                "remaining_work": [],
                "diagnostics": [{"kind": "control", "message": "single request handled synthesis and decision"}],
            }
        elif "Execute exactly one TaskBoard card" in text:
            payload = {
                "status": "failed",
                "answer": "legacy child execution should not run for control cards",
                "remaining_work": ["unexpected legacy path"],
            }
        elif "Synthesize the final result for this TaskBoard task" in text:
            payload = {
                "accepted": True,
                "reason": "control-card deliverable satisfies the criterion",
                "final_result": "# Control Result",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("# Control Result")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardSectionedArtifactRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardSectionedArtifactRequester"
    tail_marker = "SECTIONED-ARTIFACT-END-MARKER"
    first_section = (
        "# Sectioned Report\n\n"
        "This report is intentionally long enough to require a Workspace-backed sectioned artifact.\n\n"
        + ("Source-grounded analysis paragraph with bounded evidence refs.\n" * 120)
    )
    second_section = (
        "The second section carries the complete body that should not be streamed back through "
        "TaskBoard tick payloads or finalizer hot input.\n\n"
        + ("Detailed finding with supporting source boundary.\n" * 120)
        + tail_marker
    )
    full_report = f"{first_section}\n\n## Details\n\n{second_section}"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan a TaskBoard for this submitted task" in text:
            payload = {
                "board_goal": "Write a sectioned report through a control card.",
                "cards": [
                    {
                        "id": "synthesize",
                        "action_block": "Synthesize the final sectioned deliverable.",
                        "objective": "Produce the complete sectioned Markdown deliverable.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The complete sectioned report is available in Workspace.",
                        "allowed_execution_shape": "control",
                    }
                ],
                "reflection_points": ["Ensure the artifact is complete and backed by Workspace readback."],
                "completion_gate": "The sectioned artifact is written and accepted.",
                "why_this_effort_shape": "One control card can write the final sectioned report.",
                "risk_notes": [],
            }
        elif "Execute one TaskBoard control card with a single structured model request" in text:
            payload = {
                "status": "completed",
                "answer": "sectioned report manifest prepared",
                "artifact_manifest": {
                    "path": "final.md",
                    "sections": [
                        {"id": "summary", "title": "Summary", "content": self.first_section},
                        {"id": "details", "title": "Details", "content": self.second_section},
                    ],
                },
                "sufficient": True,
                "next_board_action": "finalize",
                "gaps": [],
                "evidence": ["sectioned evidence summary"],
                "remaining_work": [],
                "diagnostics": [{"kind": "control", "message": "sectioned manifest returned"}],
            }
        elif "Synthesize the final result for this TaskBoard task" in text:
            payload = {
                "accepted": True,
                "reason": "sectioned Workspace artifact satisfies the criterion",
                "final_result": "# Sectioned Report",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("# Sectioned Report")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardFinalCandidateRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardFinalCandidateRequester"
    full_report = (
        "# Repository Report\n\n"
        "## Repository Snapshot\n"
        "- Source: cloned repository files.\n\n"
        "## Purpose\n"
        "This project trains reusable agent skills from source-grounded examples.\n\n"
        "## Core Ideas\n"
        "- Treat the skill document as trainable state.\n"
        "- Validate edits against held-out tasks.\n\n"
        "## Evidence Table\n"
        "| Claim | Evidence |\n"
        "| --- | --- |\n"
        "| Skill state is edited | README.md and package entry point |\n"
    )

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan a TaskBoard for this submitted task" in text:
            payload = {
                "board_goal": "Write a source-grounded repository report.",
                "cards": [
                    {
                        "id": "final_report",
                        "action_block": "Draft the final repository report.",
                        "objective": "Produce the complete final Markdown deliverable.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The complete final report is available.",
                        "allowed_execution_shape": "model",
                    }
                ],
                "reflection_points": [],
                "completion_gate": "The final report is complete.",
                "why_this_effort_shape": "One card is enough for this regression.",
                "risk_notes": [],
            }
        elif "Execute exactly one TaskBoard card" in text:
            payload = {
                "status": "completed",
                "answer": self.full_report,
                "evidence": ["README.md supports the report."],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif "Synthesize the final result for this TaskBoard task" in text:
            payload = {
                "accepted": True,
                "reason": "all required sections are present",
                "final_result": self.full_report[:120],
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload(self.full_report)
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardSlowCardRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardSlowCardRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan a TaskBoard for this submitted task" in text:
            payload = {
                "board_goal": "Exercise a bounded card timeout.",
                "cards": [
                    {
                        "id": "slow",
                        "action_block": "Run a slow evidence collection step.",
                        "objective": "Collect evidence with a slow model request.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The slow evidence is collected.",
                        "allowed_execution_shape": "model",
                    }
                ],
                "reflection_points": [],
                "completion_gate": "The slow card completes.",
                "why_this_effort_shape": "One card is enough for this timeout probe.",
                "risk_notes": [],
            }
        elif "Execute exactly one TaskBoard card" in text:
            await asyncio.sleep(0.6)
            payload = {
                "status": "completed",
                "answer": "slow card eventually completed",
                "evidence": ["late evidence"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif "Synthesize the final result for this TaskBoard task" in text:
            payload = {
                "accepted": True,
                "reason": "should not finalize after a card timeout",
                "final_result": "unexpected final",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("unexpected final")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardRetryCardRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardRetryCardRequester"
    card_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockTaskBoardRetryCardRequester.card_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan a TaskBoard for this submitted task" in text:
            payload = {
                "board_goal": "Exercise card retry.",
                "cards": [
                    {
                        "id": "retry",
                        "action_block": "Collect evidence with one transient timeout.",
                        "objective": "Collect retry evidence.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "Retry evidence is collected.",
                        "allowed_execution_shape": "model",
                    }
                ],
                "reflection_points": [],
                "completion_gate": "The retry card completes.",
                "why_this_effort_shape": "One retried card is enough for this regression.",
                "risk_notes": [],
            }
        elif "Execute exactly one TaskBoard card" in text:
            MockTaskBoardRetryCardRequester.card_calls += 1
            if MockTaskBoardRetryCardRequester.card_calls == 1:
                await asyncio.sleep(0.5)
            payload = {
                "status": "completed",
                "answer": "retried card completed",
                "evidence": ["retry evidence"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif "Synthesize the final result for this TaskBoard task" in text:
            payload = {
                "accepted": True,
                "reason": "retry card evidence satisfies the criterion",
                "final_result": "taskboard retry accepted result",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("taskboard retry accepted result")
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
                        "allowed_execution_shape": "readback",
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
            if (
                "Review evidence by reading cold artifact refs" in text
                and "read_action_artifact" in text
                and artifact_id_match is not None
            ):
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
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("taskboard readback accepted result")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardDependencyReadbackRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardDependencyReadbackRequester"
    last_action_id = ""
    dependency_readback_seen = False
    source_refs_seen = False

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockTaskBoardDependencyReadbackRequester.last_action_id = ""
        MockTaskBoardDependencyReadbackRequester.dependency_readback_seen = False
        MockTaskBoardDependencyReadbackRequester.source_refs_seen = False

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan a TaskBoard for this submitted task" in text:
            payload = {
                "board_goal": "Complete the task through automatic dependency readback.",
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
                        "id": "synthesize",
                        "action_block": "Use dependency evidence.",
                        "objective": "Use the dependency evidence without a dedicated readback card.",
                        "depends_on": ["collect"],
                        "evidence_to_use": ["collect"],
                        "done_when": "The hidden evidence detail is used.",
                        "allowed_execution_shape": "model",
                    },
                ],
                "reflection_points": ["Use cold refs when dependency previews are insufficient."],
                "completion_gate": "Both cards completed and final answer synthesized.",
                "why_this_effort_shape": "The second card depends on the first card evidence.",
                "risk_notes": [],
            }
        elif "next_action" in text and "execution_commands" in text:
            if (
                "dependency_readbacks" in text
                and "Hidden evidence" in text
            ):
                MockTaskBoardDependencyReadbackRequester.dependency_readback_seen = True
                if "source_refs" in text and "https://example.test/evidence" in text:
                    MockTaskBoardDependencyReadbackRequester.source_refs_seen = True
                payload = {"next_action": "response", "execution_commands": []}
            else:
                MockTaskBoardDependencyReadbackRequester.last_action_id = "produce_large_evidence"
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
            payload = {
                "status": "completed",
                "answer": "cold artifact produced",
                "evidence": ["produce_large_evidence produced a cold artifact ref"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif "Execute exactly one TaskBoard card" in text and "Use dependency evidence without a dedicated readback card" in text:
            if "dependency_readbacks" in text and "Hidden evidence" in text:
                MockTaskBoardDependencyReadbackRequester.dependency_readback_seen = True
                if "source_refs" in text and "https://example.test/evidence" in text:
                    MockTaskBoardDependencyReadbackRequester.source_refs_seen = True
                payload = {
                    "status": "completed",
                    "answer": "dependency readback evidence used",
                    "evidence": ["dependency_readbacks included Hidden evidence"],
                    "remaining_work": [],
                    "diagnostics": [],
                }
            else:
                payload = {
                    "status": "blocked",
                    "answer": "dependency readback missing",
                    "evidence": [],
                    "remaining_work": ["Need dependency artifact readback."],
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
                "reason": "dependency evidence was read back before the downstream card.",
                "final_result": "taskboard dependency readback accepted result",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("taskboard dependency readback accepted result")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardControlDependencyReadbackRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardControlDependencyReadbackRequester"
    dependency_readback_seen = False
    source_refs_seen = False

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockTaskBoardControlDependencyReadbackRequester.dependency_readback_seen = False
        MockTaskBoardControlDependencyReadbackRequester.source_refs_seen = False

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan a TaskBoard for this submitted task" in text:
            payload = {
                "board_goal": "Complete the task through control-card dependency readback.",
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
                        "id": "synthesize",
                        "action_block": "Synthesize from dependency evidence.",
                        "objective": "Synthesize after reading dependency evidence.",
                        "depends_on": ["collect"],
                        "evidence_to_use": ["collect"],
                        "done_when": "The hidden evidence detail is included in the synthesis.",
                        "allowed_execution_shape": "control",
                    },
                ],
                "reflection_points": ["Use cold refs when dependency previews are insufficient."],
                "completion_gate": "Both cards completed and final answer synthesized.",
                "why_this_effort_shape": "The control card depends on collected evidence.",
                "risk_notes": [],
            }
        elif "next_action" in text and "execution_commands" in text:
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
            payload = {
                "status": "completed",
                "answer": "cold artifact produced",
                "evidence": ["produce_large_evidence produced a cold artifact ref"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif "Execute one TaskBoard control card" in text:
            if "dependency_readbacks" in text and "Hidden evidence" in text:
                MockTaskBoardControlDependencyReadbackRequester.dependency_readback_seen = True
                if "source_refs" in text and "https://example.test/evidence" in text:
                    MockTaskBoardControlDependencyReadbackRequester.source_refs_seen = True
                payload = {
                    "status": "completed",
                    "answer": "control dependency readback evidence used",
                    "candidate_final_result": "control dependency readback accepted result",
                    "sufficient": True,
                    "next_board_action": "finalize",
                    "gaps": [],
                    "evidence": ["dependency_readbacks included Hidden evidence"],
                    "remaining_work": [],
                    "diagnostics": [],
                }
            else:
                payload = {
                    "status": "blocked",
                    "answer": "dependency readback missing",
                    "sufficient": False,
                    "next_board_action": "readback",
                    "gaps": ["Need dependency artifact readback."],
                    "evidence": [],
                    "remaining_work": ["Need dependency artifact readback."],
                    "diagnostics": [],
                }
        elif "Synthesize the final result for this TaskBoard task" in text:
            payload = {
                "accepted": True,
                "reason": "control-card dependency evidence was read back.",
                "final_result": "taskboard control dependency readback accepted result",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("taskboard control dependency readback accepted result")
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


def _create_flat_repair_constraint_agent(name: str = "agent-execution-flat-repair-constraint"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatRepairConstraintRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_action_agent(name: str = "agent-execution-flat-action"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatActionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_action_planning_stall_agent(name: str = "agent-execution-flat-action-planning-stall"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatActionPlanningStallRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_action_planning_slow_agent(name: str = "agent-execution-flat-action-planning-slow"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatActionPlanningSlowRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_workspace_artifact_draft_stall_agent(name: str = "agent-execution-artifact-draft-stall"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockWorkspaceArtifactDraftStallRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_parallel_action_agent(name: str = "agent-execution-flat-parallel-action"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatParallelActionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_evidence_candidate_agent(name: str = "agent-execution-flat-evidence-candidate"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatEvidenceCandidateRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_agent(name: str = "agent-execution-taskboard"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_control_agent(name: str = "agent-execution-taskboard-control"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardControlRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_sectioned_artifact_agent(name: str = "agent-execution-taskboard-sectioned-artifact"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardSectionedArtifactRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_final_candidate_agent(name: str = "agent-execution-taskboard-final-candidate"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardFinalCandidateRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_slow_card_agent(name: str = "agent-execution-taskboard-slow-card"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardSlowCardRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_retry_card_agent(name: str = "agent-execution-taskboard-retry-card"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardRetryCardRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_readback_agent(name: str = "agent-execution-taskboard-readback"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardReadbackRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_dependency_readback_agent(name: str = "agent-execution-taskboard-dependency-readback"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardDependencyReadbackRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_control_dependency_readback_agent(
    name: str = "agent-execution-taskboard-control-dependency-readback",
):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardControlDependencyReadbackRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def test_taskboard_control_readback_action_auto_patch_adds_continuation():
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="auto-readback",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "auto-readback-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect source evidence."},
                    {
                        "id": "final",
                        "objective": "Write final answer after full source readback.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                        "required_outputs": ["final.md"],
                    },
                ],
            }
        ),
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {"op": "record_card_result", "result": {"card_id": "collect", "status": "completed"}}
            ],
        },
    )
    card = revision.graph.card_by_id()["final"]
    patch = AgentTask._taskboard_control_auto_patch(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "gaps": ["Need complete source page."],
            "remaining_work": ["Generate final.md after readback."],
        },
    )

    assert patch is not None
    next_revision = validator.apply_patch(revision, patch)
    cards = next_revision.graph.card_by_id()
    assert cards["final"].failure_policy == "degradable"
    assert "final.readback" in cards
    assert "final.continue" in cards
    assert cards["final.readback"].allowed_execution_shape == "readback"
    assert cards["final.continue"].depends_on == ("collect", "final.readback")
    schedule = validator.schedule(next_revision)
    assert "final.readback" in schedule.runnable_card_ids


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


def test_taskboard_final_normalization_preserves_complete_workspace_candidate():
    candidate = "# Full Report\n\n" + ("complete section body\n" * 120)
    final = {
        "accepted": True,
        "reason": "The report is complete.",
        "final_result": "The full report has been written to final.md.",
        "missing_criteria": [],
    }

    normalized = AgentTask._normalize_taskboard_final_result(final, candidate)

    assert normalized["final_result"] == candidate.strip()


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
async def test_structured_deliverable_contract_requires_workspace_readback(tmp_path):
    agent = _create_goal_pursuit_agent("execution-deliverable-contract-guard").use_workspace(tmp_path / "workspace")

    execution = agent.create_task(
        goal="Produce the requested final file.",
        success_criteria=["The final deliverable file exists."],
        execution="flat",
        max_iterations=1,
    )
    execution.input(
        {
            "output_contract": {
                "deliverables": [{"path": "final.md", "media_type": "text/markdown"}],
            }
        }
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    verification = task_meta["iterations"][0]["verification"]

    assert result["accepted"] is False
    assert result["status"] == "max_iterations"
    assert verification["is_complete"] is False
    assert "required_workspace_deliverable_missing" in verification["guard_reasons"]
    assert "Missing required Workspace deliverable(s): final.md" in verification["missing_criteria"]


@pytest.mark.asyncio
async def test_flat_verifier_repair_constraints_feed_next_planner(tmp_path):
    agent = _create_flat_repair_constraint_agent("execution-flat-repair-constraints").use_workspace(
        tmp_path / "workspace"
    )

    execution = agent.create_task(
        goal="Produce an Agent engineering weekly report with 5-8 news items.",
        success_criteria=["The final report contains 5-8 news items."],
        execution="flat",
        max_iterations=2,
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    first_verification = task_meta["iterations"][0]["verification"]
    second_plan = task_meta["iterations"][1]["plan"]
    second_plan_prompt = MockFlatRepairConstraintRequester.second_plan_prompt

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "repaired accepted result"
    assert first_verification["is_complete"] is False
    assert first_verification["failure_analysis"] == "The candidate artifact overshoots the accepted item count."
    assert "The report must include 5-8 news items, not 15." in first_verification["acceptance_delta"]
    assert "Reduce the report to 5-8 news items." in first_verification["repair_constraints"]
    assert "The report must include 5-8 news items, not 15." in first_verification["repair_constraints"]
    assert (
        "Revise the candidate report; do not restart evidence gathering."
        in first_verification["next_step_requirements"]
    )
    assert (
        "Revise the report to satisfy the item-count constraint."
        in first_verification["next_step_requirements"]
    )
    assert second_plan["step_instruction"].startswith("Revise the candidate report")
    assert "repair_context" in second_plan_prompt
    assert "advisory_repair_constraints" in second_plan_prompt
    assert "acceptance_delta" in second_plan_prompt
    assert "Reduce the report to 5-8 news items." in second_plan_prompt
    assert "Revise the candidate report; do not restart evidence gathering." in second_plan_prompt


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
async def test_flat_request_timeout_does_not_cancel_progressing_child_execution(tmp_path):
    agent = _create_flat_action_planning_slow_agent("execution-flat-action-planning-slow").use_workspace(
        tmp_path / "workspace"
    )

    @agent.action_func
    def probe_action() -> dict[str, str]:
        return {"status": "ok"}

    execution = (
        agent.create_task(
            goal="Collect action evidence.",
            success_criteria=["The probe action executes."],
            execution="flat",
            max_iterations=1,
            options={
                "request_timeout_seconds": 0.2,
                "agent_task": {"request_timeout_seconds": 0.2},
            },
        )
        .use_actions(["probe_action"])
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "flat action accepted result"
    assert not task_meta["diagnostics"].get("execution_errors")


@pytest.mark.asyncio
async def test_flat_action_planning_stall_returns_structured_child_failure(tmp_path):
    agent = _create_flat_action_planning_stall_agent("execution-flat-action-planning-timeout").use_workspace(
        tmp_path / "workspace"
    )

    @agent.action_func
    def probe_action() -> dict[str, str]:
        return {"status": "ok"}

    execution = (
        agent.create_task(
            goal="Collect action evidence.",
            success_criteria=["The probe action executes."],
            execution="flat",
            max_iterations=1,
            limits={"max_no_progress_seconds": 0.2},
        )
        .use_actions(["probe_action"])
    )

    started_at = asyncio.get_running_loop().time()
    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    elapsed = asyncio.get_running_loop().time() - started_at
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    execution_error = task_meta["diagnostics"]["execution_errors"][0]

    assert elapsed < 2
    assert result["status"] == "max_iterations"
    assert result["accepted"] is False
    assert execution_error["type"] != "_AgentTaskDeadlineExceeded"
    assert (
        "max_no_progress_seconds" in execution_error["message"]
        or "no progress" in execution_error["message"]
        or "stalled" in execution_error["message"]
    )
    assert task_meta["iterations"][0]["execution_meta"]["status"] == "failed"
    assert task_meta["iterations"][0]["verification"]["is_complete"] is False


@pytest.mark.asyncio
async def test_workspace_artifact_draft_timeout_emits_heartbeat_and_diagnostics(tmp_path):
    agent = _create_workspace_artifact_draft_stall_agent("execution-workspace-artifact-draft-timeout").use_workspace(
        tmp_path / "workspace"
    )

    execution = agent.create_task(
        goal="Produce final.md.",
        success_criteria=["final.md is written and read back."],
        execution="flat",
        max_iterations=1,
        options={
            "request_timeout_seconds": 0.2,
            "agent_task": {
                "request_timeout_seconds": 0.2,
                "heartbeat_interval_seconds": 0.05,
            },
        },
    )

    async def collect_stream() -> list[Any]:
        return [item async for item in execution.get_async_generator(type="instant")]

    started_at = asyncio.get_running_loop().time()
    stream_task = asyncio.create_task(collect_stream())
    result = await execution.async_get_data()
    stream_items = await stream_task
    meta = await execution.async_get_meta()
    elapsed = asyncio.get_running_loop().time() - started_at
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    deliveries = task_meta["diagnostics"]["workspace_artifact_delivery"]
    failed_delivery = deliveries[-1]
    heartbeat_items = [item for item in stream_items if getattr(item, "path", "") == "agent_task.heartbeat"]

    assert elapsed < 2
    assert result["accepted"] is False
    assert failed_delivery["status"] == "failed"
    assert failed_delivery["error"]["type"] == "_AgentTaskDeadlineExceeded"
    assert "workspace_artifact_draft stream produced no event" in failed_delivery["error"]["message"]
    assert heartbeat_items
    assert any(
        getattr(item, "value", {}).get("stage") == "workspace_artifact_draft"
        for item in heartbeat_items
        if isinstance(getattr(item, "value", None), dict)
    )
    assert not (tmp_path / "workspace" / "final.md").exists()


@pytest.mark.asyncio
async def test_flat_actions_shape_fans_out_multiple_commands_in_one_step(tmp_path):
    agent = _create_flat_parallel_action_agent("execution-flat-action-fanout").use_workspace(tmp_path / "workspace")
    active = 0
    max_active = 0

    async def run_probe(label: str) -> dict[str, str]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"status": "ok", "label": label}

    @agent.action_func
    async def slow_a() -> dict[str, str]:
        return await run_probe("a")

    @agent.action_func
    async def slow_b() -> dict[str, str]:
        return await run_probe("b")

    execution = (
        agent.create_task(
            goal="Collect two independent action evidence records.",
            success_criteria=["Both independent action results are collected."],
            execution="flat",
            max_iterations=1,
        )
        .use_actions(["slow_a", "slow_b"])
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    action_logs = task_meta["iterations"][0]["execution_meta"]["logs"]["action_logs"]
    if isinstance(action_logs, dict):
        action_ids = list(action_logs.keys())
    else:
        action_ids = [item.get("action_id") for item in action_logs]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert max_active == 2
    assert set(action_ids) == {"slow_a", "slow_b"}


@pytest.mark.asyncio
async def test_flat_verifier_uses_bounded_action_evidence_prompt(tmp_path):
    agent = _create_flat_action_agent("execution-flat-verifier-evidence-bounds").use_workspace(tmp_path / "workspace")

    hidden_tail = "VERIFIER_SHOULD_NOT_SEE_FULL_ACTION_OUTPUT"

    @agent.action_func
    def probe_action() -> dict[str, str]:
        return {
            "status": "ok",
            "payload": ("x" * 8000) + hidden_tail + ("z" * 8000),
        }

    execution = (
        agent.create_task(
            goal="Collect action evidence without flooding the verifier.",
            success_criteria=["The probe action executes."],
            execution="flat",
            max_iterations=1,
        )
        .use_actions(["probe_action"])
    )

    result = await execution.async_get_data()
    verify_requests = [
        request
        for request in MockAgentExecutionRequester.requests
        if "Verify the task against every success criterion" in request
    ]

    assert result["accepted"] is True
    assert verify_requests
    verify_prompt = verify_requests[-1]
    assert "probe_action" in verify_prompt
    assert "artifact_refs" in verify_prompt
    assert hidden_tail not in verify_prompt
    assert len(verify_prompt) < 80000


@pytest.mark.asyncio
async def test_flat_promotes_report_like_evidence_to_candidate_final_result(tmp_path):
    agent = _create_flat_evidence_candidate_agent("execution-flat-evidence-candidate").use_workspace(
        tmp_path / "workspace"
    )

    execution = agent.create_task(
        goal="Produce a Markdown weekly report.",
        success_criteria=["The final report is returned."],
        execution="flat",
        max_iterations=1,
    )

    result = await execution.async_get_data()
    verify_requests = [
        request
        for request in MockAgentExecutionRequester.requests
        if "Verify the task against every success criterion" in request
    ]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"].strip() == MockFlatEvidenceCandidateRequester.report.strip()
    assert verify_requests
    assert "candidate_final_result" in verify_requests[-1]
    assert "Weekly Report" in verify_requests[-1]


@pytest.mark.asyncio
async def test_taskboard_execution_strategy_runs_framework_owned_board(tmp_path):
    agent = _create_taskboard_agent("execution-taskboard-strategy").use_workspace(tmp_path / "workspace")

    execution = agent.create_task(
        goal="Produce a board-managed answer.",
        success_criteria=["The board final answer is accepted."],
        execution="taskboard",
        max_iterations=2,
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = task_meta["result"]["taskboard"]
    phases = task_meta["diagnostics"]["phases"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["execution_strategy"] == "taskboard"
    assert result["final_result"] == "taskboard accepted result"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["task_refs"]["execution_strategy"] == "taskboard"
    assert task_meta["execution_strategy"] == "taskboard"
    lifecycle_phase = next(phase for phase in phases if phase["phase"] == "taskboard_lifecycle_started")
    tick_phase = next(phase for phase in phases if phase["phase"] == "taskboard_tick")
    assert lifecycle_phase["diagnostics"]["runtime_topology"]["driver"] == "triggerflow_taskboard_lifecycle"
    assert tick_phase["diagnostics"]["runtime_topology"]["driver"] == "triggerflow_taskboard_lifecycle"
    assert tick_phase["diagnostics"]["runtime_topology"]["tick"]["fanout"] == "signal_net_dynamic_overlay"
    assert taskboard["revision"]["card_results"]["collect"]["status"] == "completed"
    assert taskboard["evidence_view"]["cards"][0]["card_id"] == "collect"
    assert "content" not in taskboard["evidence_view"]["cards"][0]["artifact_refs"]
    assert any(item.path == "agent_task.taskboard.card.collect.execution.started" for item in stream_items)
    assert any(
        item.path == "agent_task.taskboard.card.collect.execution.route.selected"
        and (item.meta or {}).get("stream_kind") == "child_execution"
        and (item.meta or {}).get("stage") == "taskboard_card"
        and (item.meta or {}).get("card_id") == "collect"
        for item in stream_items
    )


@pytest.mark.asyncio
async def test_taskboard_control_card_uses_single_model_request_without_child_execution(tmp_path):
    agent = _create_taskboard_control_agent("execution-taskboard-control-card").use_workspace(tmp_path / "workspace")

    execution = agent.create_task(
        goal="Produce a control-card deliverable.",
        success_criteria=["The control card final answer is accepted."],
        execution="taskboard",
        max_iterations=2,
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = task_meta["result"]["taskboard"]
    card_result = taskboard["revision"]["card_results"]["synthesize"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "# Control Result\n\nComplete deliverable body."
    assert card_result["status"] == "completed"
    assert card_result["metadata"]["execution_kind"] == "taskboard_control_request"
    assert any(
        item.path == "agent_task.taskboard.card.synthesize.control.started"
        for item in stream_items
    )
    assert any(
        (item.meta or {}).get("stream_kind") == "taskboard_control_request"
        and (item.meta or {}).get("card_id") == "synthesize"
        for item in stream_items
    )
    assert not any(
        item.path == "agent_task.taskboard.card.synthesize.execution.started"
        for item in stream_items
    )
    assert not any("Execute exactly one TaskBoard card" in request for request in MockAgentExecutionRequester.requests)
    planning_requests = [
        request for request in MockAgentExecutionRequester.requests
        if "Plan a TaskBoard for this submitted task" in request
    ]
    assert planning_requests
    assert "serial chain of control-only cards" in planning_requests[-1]
    assert "control_card_guidance" in planning_requests[-1]


@pytest.mark.asyncio
async def test_taskboard_sectioned_artifact_uses_workspace_and_bounded_stream(tmp_path):
    agent = _create_taskboard_sectioned_artifact_agent("execution-taskboard-sectioned-artifact").use_workspace(
        tmp_path / "workspace"
    )

    execution = agent.create_task(
        goal="Produce a sectioned final report.",
        success_criteria=["The complete sectioned final report is written and accepted."],
        execution="taskboard",
        max_iterations=2,
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = task_meta["result"]["taskboard"]
    deliveries = task_meta["diagnostics"]["workspace_artifact_delivery"]
    delivered = next(item for item in deliveries if item.get("status") == "delivered")
    tick_completed_items = [
        item for item in stream_items if str(getattr(item, "path", "")).endswith(".completed")
        and ".taskboard.tick." in str(getattr(item, "path", ""))
    ]
    final_requests = [
        request for request in MockAgentExecutionRequester.requests
        if "Synthesize the final result for this TaskBoard task" in request
    ]
    marker = MockTaskBoardSectionedArtifactRequester.tail_marker
    expected_workspace_body = (
        f"{MockTaskBoardSectionedArtifactRequester.first_section.strip()}\n\n"
        f"## Details\n\n{MockTaskBoardSectionedArtifactRequester.second_section.strip()}"
    )

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert marker in result["final_result"]
    assert delivered["mode"] == "sectioned_workspace_artifact"
    assert delivered["readback"]["bytes"] == len(expected_workspace_body.encode("utf-8"))
    assert delivered["readback"]["sha256"]
    assert taskboard["revision"]["card_results"]["synthesize"]["preview"]["workspace_artifact_delivery"]["mode"] == (
        "sectioned_workspace_artifact"
    )
    assert tick_completed_items
    for item in tick_completed_items:
        value_text = json.dumps(DataFormatter.sanitize(getattr(item, "value", None)), ensure_ascii=False)
        assert marker not in value_text
        assert len(value_text) < 20000
    assert final_requests
    assert marker not in final_requests[-1]


@pytest.mark.asyncio
async def test_taskboard_final_preserves_complete_candidate_from_terminal_card(tmp_path):
    agent = _create_taskboard_final_candidate_agent("execution-taskboard-final-candidate").use_workspace(
        tmp_path / "workspace"
    )

    execution = agent.create_task(
        goal="Write the complete repository report.",
        success_criteria=["The final result preserves the complete Markdown deliverable."],
        execution="taskboard",
        max_iterations=2,
    )

    result = await execution.async_get_data()

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == MockTaskBoardFinalCandidateRequester.full_report.strip()
    assert len(result["final_result"]) > len(MockTaskBoardFinalCandidateRequester.full_report[:120])


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

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = task_meta["result"]["taskboard"]
    review_result = taskboard["revision"]["card_results"]["review"]
    readbacks = review_result["preview"]["readbacks"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "taskboard readback accepted result"
    assert review_result["status"] == "completed"
    assert review_result["metadata"]["execution_kind"] == "taskboard_artifact_readback"
    assert readbacks[0]["ok"] is True
    assert "Hidden evidence" in json.dumps(readbacks[0]["value_preview"], ensure_ascii=False)
    assert any(item.path == "agent_task.taskboard.card.review.readback.started" for item in stream_items)
    assert any(item.path == "agent_task.taskboard.card.review.readback.completed" for item in stream_items)
    assert not any(item.path == "agent_task.taskboard.card.review.execution.started" for item in stream_items)


@pytest.mark.asyncio
async def test_taskboard_agent_card_prefetches_dependency_action_artifact_refs(tmp_path):
    agent = _create_taskboard_dependency_readback_agent(
        "execution-taskboard-dependency-readback"
    ).use_workspace(tmp_path / "workspace")

    @agent.action_func
    def produce_large_evidence() -> dict[str, Any]:
        return {
            "records": [
                {
                    "title": "Hidden evidence",
                    "url": "https://example.test/evidence",
                    "snippet": "detail available only through automatic dependency readback",
                }
            ],
            "padding": "x" * 9000,
        }

    execution = (
        agent.create_task(
            goal="Use a dependency cold artifact without a dedicated readback card.",
            success_criteria=["The downstream card uses dependency readback evidence."],
            execution="taskboard",
            max_iterations=3,
        )
        .use_actions(["produce_large_evidence"])
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = task_meta["result"]["taskboard"]
    synthesize_result = taskboard["revision"]["card_results"]["synthesize"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "taskboard dependency readback accepted result"
    assert synthesize_result["status"] == "completed"
    assert MockTaskBoardDependencyReadbackRequester.dependency_readback_seen is True
    assert MockTaskBoardDependencyReadbackRequester.source_refs_seen is True
    assert any(
        item.path == "agent_task.taskboard.card.synthesize.dependency_readback.started"
        for item in stream_items
    )
    assert any(
        item.path == "agent_task.taskboard.card.synthesize.dependency_readback.completed"
        for item in stream_items
    )


@pytest.mark.asyncio
async def test_taskboard_control_card_prefetches_dependency_action_artifact_refs(tmp_path):
    agent = _create_taskboard_control_dependency_readback_agent(
        "execution-taskboard-control-dependency-readback"
    ).use_workspace(tmp_path / "workspace")

    @agent.action_func
    def produce_large_evidence() -> dict[str, Any]:
        return {
            "records": [
                {
                    "title": "Hidden evidence",
                    "url": "https://example.test/evidence",
                    "snippet": "detail available only through automatic control-card dependency readback",
                }
            ],
            "padding": "x" * 9000,
        }

    execution = (
        agent.create_task(
            goal="Use a dependency cold artifact in a control synthesis card.",
            success_criteria=["The control card uses dependency readback evidence."],
            execution="taskboard",
            max_iterations=3,
        )
        .use_actions(["produce_large_evidence"])
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = task_meta["result"]["taskboard"]
    synthesize_result = taskboard["revision"]["card_results"]["synthesize"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "taskboard control dependency readback accepted result"
    assert synthesize_result["status"] == "completed"
    assert MockTaskBoardControlDependencyReadbackRequester.dependency_readback_seen is True
    assert MockTaskBoardControlDependencyReadbackRequester.source_refs_seen is True
    assert any(
        item.path == "agent_task.taskboard.card.synthesize.dependency_readback.started"
        for item in stream_items
    )
    assert any(
        item.path == "agent_task.taskboard.card.synthesize.dependency_readback.completed"
        for item in stream_items
    )


@pytest.mark.asyncio
async def test_taskboard_card_timeout_returns_structured_card_failure(tmp_path):
    agent = _create_taskboard_slow_card_agent("execution-taskboard-card-timeout").use_workspace(tmp_path / "workspace")

    execution = agent.create_task(
        goal="Run a board card that should time out.",
        success_criteria=["The board records a structured card timeout."],
        execution="taskboard",
        max_iterations=1,
        options={
            "request_timeout_seconds": 5.0,
            "agent_task": {"taskboard_card_timeout_seconds": 0.25},
        },
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = task_meta["result"]["taskboard"]
    slow_result = taskboard["revision"]["card_results"]["slow"]
    diagnostic = slow_result["diagnostics"][0]

    assert result["status"] == "error"
    assert result["accepted"] is False
    assert result["artifact_status"] == "partial"
    assert result["taskboard"]["revision"]["card_results"]["slow"]["status"] == "failed"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["task_refs"]["execution_strategy"] == "taskboard"
    assert slow_result["status"] == "failed"
    assert diagnostic["code"] == "taskboard.card.timeout"
    assert diagnostic["card_id"] == "slow"
    assert diagnostic["timeout_seconds"] == 0.25
    assert "timed out" in diagnostic["message"]
    assert task_meta["diagnostics"]["taskboard_card_errors"][0]["code"] == "taskboard.card.timeout"


@pytest.mark.asyncio
async def test_taskboard_tick_timeout_does_not_cancel_running_cards(tmp_path):
    agent = _create_taskboard_slow_card_agent("execution-taskboard-tick-timeout-does-not-cancel").use_workspace(
        tmp_path / "workspace"
    )

    execution = agent.create_task(
        goal="Run a slow TaskBoard card without tick-level cancellation.",
        success_criteria=["The slow card is allowed to finish under its card timeout."],
        execution="taskboard",
        max_iterations=1,
        options={
            "request_timeout_seconds": 5.0,
            "agent_task": {
                "taskboard_tick_timeout_seconds": 0.05,
                "taskboard_card_timeout_seconds": 1.5,
            },
        },
    )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    revision = task_meta["result"]["taskboard"]["revision"]
    slow_result = revision["card_results"]["slow"]

    assert result["status"] == "completed"
    assert slow_result["status"] == "completed"
    assert slow_result["preview"]["answer"] == "slow card eventually completed"
    assert not any(
        isinstance(diagnostic, dict) and diagnostic.get("code") == "taskboard.tick.card_interrupted"
        for diagnostic in revision.get("diagnostics", [])
    )


@pytest.mark.asyncio
async def test_taskboard_card_transient_timeout_retries_and_completes(tmp_path):
    agent = _create_taskboard_retry_card_agent("execution-taskboard-card-retry").use_workspace(tmp_path / "workspace")

    execution = agent.create_task(
        goal="Run a board card that should recover after one transient timeout.",
        success_criteria=["The board records retry evidence and still returns a final result."],
        execution="taskboard",
        max_iterations=2,
        options={
            "request_timeout_seconds": 5.0,
            "agent_task": {
                "taskboard_card_timeout_seconds": 0.3,
                "taskboard_card_max_attempts": 2,
            },
        },
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = task_meta["result"]["taskboard"]
    retry_result = taskboard["revision"]["card_results"]["retry"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "taskboard retry accepted result"
    assert retry_result["status"] == "completed"
    assert retry_result["metadata"]["attempt_index"] == 2
    assert retry_result["metadata"]["max_attempts"] == 2
    assert task_meta["diagnostics"]["taskboard_card_retries"][0]["code"] == "taskboard.card.timeout"
    assert any(item.path == "agent_task.taskboard.card.retry.execution.retry" for item in stream_items)


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

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    meta = await execution.async_get_meta()

    assert type(execution).__name__ == "AgentExecution"
    assert execution.goal_items == ["Build the site."]
    assert execution.success_criteria_items == ["The runnable page exists."]
    assert execution.prompt_snapshot["input"] == "Use the supplied product facts."
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["effective_options"]["effort_strategy"]["max_iterations"] == 1
    assert any(item.path == "agent_task.phase.configured" for item in stream_items)
    assert any(item.path == "agent_task.phase.terminal" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "child_execution" for item in stream_items)


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
        .create_execution(limits={"max_seconds": 0.2, "max_no_progress_seconds": 5})
        .goal("Build the site.", success_criteria=["The runnable page exists."])
        .strategy("flat")
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

    assert result["status"] in {"blocked", "max_iterations"}
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
async def test_agent_execution_context_progress_is_published_to_stream():
    agent = _create_agent("execution-progress-stream")
    execution = agent.input("progress probe").create_execution()

    execution.execution_context.record_progress(
        stage="action_runtime",
        status="started",
        event_type="action_runtime.started",
        meta={"action_id": "web_search"},
    )
    await asyncio.sleep(0)

    progress_items = [item for item in execution.stream.items if item.path == "runtime.progress.action_runtime.started"]
    assert progress_items
    item = progress_items[-1]
    assert item.source == "agent_execution"
    assert (item.meta or {})["stream_kind"] == "runtime_progress"
    assert item.value["stage"] == "action_runtime"
    assert item.value["status"] == "started"
    assert item.value["meta"]["action_id"] == "web_search"


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

    delta_chunks = [chunk async for chunk in execution.get_async_generator(type="delta")]
    assert delta_chunks
    assert all(isinstance(chunk, str) for chunk in delta_chunks)

    default_chunks = [chunk async for chunk in execution.get_async_generator()]
    assert default_chunks == delta_chunks


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
