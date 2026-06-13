from __future__ import annotations

import json
import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest

from agently import Agently
from agently.core import PluginManager
from agently.types.data import AgentlyRequestData
from agently.utils import DataFormatter, Settings
from examples.agent_task.interview_question_preparation import judge_interview_semantics


class MockAgentTaskRequester:
    name = "MockAgentTaskRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}
    calls: list[str] = []
    verification_calls = 0

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def reset():
        MockAgentTaskRequester.calls = []
        MockAgentTaskRequester.verification_calls = 0

    @staticmethod
    def _on_register():
        MockAgentTaskRequester.reset()

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"messages": self.prompt.to_messages(), "output": self.prompt.get("output")},
            request_options={"stream": True},
            request_url="mock://agent-task",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentTaskRequester.calls.append(text)
        if "Summarize AgentTask progress" in text:
            payload = {
                "message": "Progress model summarized the current snapshot.",
            }
        elif "Verify the task against every success criterion" in text:
            MockAgentTaskRequester.verification_calls += 1
            if MockAgentTaskRequester.verification_calls == 1:
                payload = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "verification evidence is incomplete",
                    "missing_criteria": ["script does not run yet"],
                    "replan_instruction": "run the repair step again with the recorded failure evidence",
                    "final_result": "",
                }
            else:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "all success criteria are now satisfied",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "legacy script upgraded and verified",
                }
        elif "Plan the next bounded AgentExecution step" in text:
            payload = {
                "step_instruction": "repair the legacy script using current Agently APIs",
                "expected_evidence": "script execution succeeds",
                "rationale": "the prior failure must be fixed before final verification",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "patched script and ran verification",
                "evidence": ["python legacy_script.py exited with status 0"],
                "remaining_work": [],
            }
        else:
            payload = {"answer": "ok"}
        yield "message", json.dumps(payload, ensure_ascii=False)

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


def _create_agent(name: str = "agent-task-loop-test"):
    settings = Settings(name=f"{name}-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-plugins")
    plugin_manager.register("ModelRequester", MockAgentTaskRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


@pytest.mark.asyncio
async def test_agent_goal_success_criteria_uses_task_execution_path(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-goal-task-path").use_workspace(tmp_path / "task-workspace")

    execution = (
        agent
        .goal(
            "Repair a legacy Agently script so it runs on the current API.",
            ["The script runs successfully."],
        )
        .strategy("task", max_iterations=2)
    )

    result = await execution.async_start()
    meta = await execution.async_get_meta()

    assert result["status"] == "completed"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["task_refs"]["task_id"]
    assert meta["task_refs"]["status"] == "completed"
    assert meta["success_criteria"] == ["The script runs successfully."]


@pytest.mark.asyncio
async def test_agent_task_loop_replans_and_records_workspace(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent()

    task = agent.create_task(
        task_id="legacy-script-upgrade",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=[
            "The original failure is recorded.",
            "The script runs successfully.",
            "Verification evidence is stored.",
        ],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
        limits={"max_model_requests": 1},
        options={"agent_task": {"stream_progress": True}},
    )

    result_facade = task.get_result()
    stream_items = [item async for item in result_facade.get_async_generator()]
    result = await result_facade.async_get_data()
    execution_meta = await result_facade.async_get_meta()
    meta = await task.meta()
    resume = await result_facade.async_resume()

    assert result["status"] == "completed"
    assert result["iterations"] == 2
    assert result_facade.task_refs["task_id"] == "legacy-script-upgrade"
    assert result_facade.task_refs["status"] == "completed"
    assert execution_meta["task_refs"]["task_id"] == "legacy-script-upgrade"
    assert execution_meta["task_refs"]["status"] == "completed"
    assert resume["supported"] is False
    assert resume["reason"] == "AgentExecution resume is reserved for resumable strategies."
    assert meta["status"] == "completed"
    assert len(meta["iterations"]) == 2
    assert MockAgentTaskRequester.verification_calls == 2
    assert any(item.path == "agent_task.started" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "progress" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)
    progress_messages = [
        item.value.get("message")
        for item in stream_items
        if (item.meta or {}).get("stream_kind") == "progress" and isinstance(item.value, dict)
    ]
    snapshot_values = [
        item.value
        for item in stream_items
        if (item.meta or {}).get("stream_kind") == "snapshot" and isinstance(item.value, dict)
    ]
    assert any("building a Workspace context pack" in str(message) for message in progress_messages)
    assert any(value.get("stage") == "plan" for value in snapshot_values)
    assert any(value.get("stage") == "verification" for value in snapshot_values)
    assert any(item.path.endswith(".replan") for item in stream_items)
    assert any(item.path == "result" for item in stream_items)
    phase_names = [item["phase"] for item in meta["diagnostics"]["phases"]]
    assert "configured" in phase_names
    assert "planned" in phase_names
    assert "executing" in phase_names
    assert "evidence_recorded" in phase_names
    assert "verified" in phase_names
    assert "guarded" in phase_names
    assert "replanned" in phase_names
    assert "terminal" in phase_names
    assert any(item.path == "agent_task.phase.verified" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "phase" for item in stream_items)
    assert len(meta["workspace_refs"]["observations"]) == 2
    assert len(meta["workspace_refs"]["decisions"]) == 2
    assert len(meta["workspace_refs"]["verification"]) == 2
    assert len(meta["workspace_refs"]["checkpoints"]) == 2
    assert len(meta["workspace_refs"]["evidence_links"]) == 6
    workspace = agent.workspace
    assert workspace is not None
    assert len(await workspace.checkpoint_history("legacy-script-upgrade")) == 2
    verifies_links = await workspace.links(relation="verifies_observation")
    decision_links = await workspace.links(relation="implements_decision")
    checkpoint_links = await workspace.links(relation="checkpointed_by")
    assert len(verifies_links) == 2
    assert len(decision_links) == 2
    assert len(checkpoint_links) == 2
    assert all(link["meta"]["evidence"] for link in [*verifies_links, *decision_links, *checkpoint_links])


@pytest.mark.asyncio
async def test_agent_task_loop_progress_stream_is_opt_in(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-opt-in")

    task = agent.create_task(
        task_id="progress-opt-in",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
    )

    stream_items = [item async for item in task.stream()]

    assert not any((item.meta or {}).get("stream_kind") == "progress" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_uses_snapshot_background(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-model")

    task = agent.create_task(
        task_id="progress-model",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "progress-narrator",
                "progress_timeout_seconds": 5,
            },
        },
    )

    stream_items = [item async for item in task.stream()]
    progress_items = [
        item
        for item in stream_items
        if (item.meta or {}).get("stream_kind") == "progress"
    ]

    assert progress_items
    assert all((item.meta or {}).get("progress_source") == "model" for item in progress_items)
    assert any("Progress model summarized" in item.value.get("message", "") for item in progress_items)
    assert not any("building a Workspace context pack" in item.value.get("message", "") for item in progress_items)
    assert any("Summarize AgentTask progress" in call for call in MockAgentTaskRequester.calls)


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_omits_developer_diagnostics(tmp_path, monkeypatch):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-safe-diagnostics")

    task = agent.create_task(
        task_id="progress-safe-diagnostics",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "progress-narrator",
                "progress_timeout_seconds": 5,
            },
        },
    )

    async def noisy_context_pack(**_kwargs):
        return {
            "goal": "Repair a legacy Agently script so it runs on the current API.",
            "profile": "auto",
            "items": [],
            "omitted": [],
            "diagnostics": {
                "fallback_reason": {
                    "type": "OperationalError",
                    "message": 'fts5: syntax error near "."; no such column: question',
                },
                "builder": "default",
            },
        }

    assert task.workspace is not None
    monkeypatch.setattr(task.workspace, "build_context", noisy_context_pack)

    stream_items = [item async for item in task.stream()]
    progress_calls = [
        call
        for call in MockAgentTaskRequester.calls
        if "Summarize AgentTask progress" in call
    ]
    meta = await task.meta()

    assert progress_calls
    assert not any("fts5" in call for call in progress_calls)
    assert not any("no such column" in call for call in progress_calls)
    assert not any("fallback_reason" in call for call in progress_calls)
    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)
    assert "progress_errors" not in meta["diagnostics"]


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_does_not_delay_stream_close(tmp_path):
    class SlowProgressRequester(MockAgentTaskRequester):
        name = "SlowProgressRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Summarize AgentTask progress" in text:
                await asyncio.sleep(10)
                yield "message", json.dumps(
                    {"message": "late progress summary"},
                    ensure_ascii=False,
                )
                return
            async for event in super().request_model(request_data):
                yield event

    settings = Settings(name="agent-task-slow-progress-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-slow-progress-plugins")
    plugin_manager.register("ModelRequester", SlowProgressRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-slow-progress")

    task = agent.create_task(
        task_id="slow-progress",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "slow-progress-narrator",
                "progress_timeout_seconds": 30,
            },
        },
    )

    stream_items = await asyncio.wait_for(
        _collect_stream(task),
        timeout=5,
    )

    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)
    assert not any((item.meta or {}).get("progress_source") == "model" for item in stream_items)


@pytest.mark.asyncio
async def test_agent_execution_dynamic_task_candidate_is_execution_local():
    async def run_task(context):
        return {"task_id": context.task.id, "value": context.graph_input["value"]}

    agent = Agently.create_agent("execution-local-dynamic-task")
    execution = (
        agent
        .create_execution()
        .input("run a local dynamic task graph")
        .use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "execution-local-dynamic-task",
                "task_schema_version": "task_dag/v1",
                "tasks": [{"id": "extract", "kind": "local", "binding": "local_handler"}],
                "semantic_outputs": {"final": "extract"},
            },
            handlers={"local_handler": run_task},
            graph_input={"value": "ok"},
        )
    )

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["semantic_outputs"]["final"]["result"]["value"] == "ok"
    assert meta["route_plan"]["selected_route"] == "dynamic_task"
    assert execution.local_dynamic_task_candidates
    assert getattr(agent, "_dynamic_task_candidates", []) == []


@pytest.mark.asyncio
async def test_agent_task_loop_executes_dag_shaped_step_without_global_candidate_leak(tmp_path):
    class DagStepVerificationRequester(MockAgentTaskRequester):
        name = "DagStepVerificationRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "the DAG step returned the required evidence",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "DAG step completed with value ok.",
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    async def run_task(context):
        return {"task_id": context.task.id, "value": context.graph_input["value"]}

    settings = Settings(name="agent-task-dag-step-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-dag-step-plugins")
    plugin_manager.register("ModelRequester", DagStepVerificationRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-dag-step")
    graph = {
        "graph_id": "agent-task-loop-dag-step",
        "task_schema_version": "task_dag/v1",
        "tasks": [{"id": "extract", "kind": "local", "binding": "local_handler"}],
        "semantic_outputs": {"final": "extract"},
    }
    task = agent.create_task(
        task_id="dag-shaped-step",
        goal="Return the final DAG result.",
        success_criteria=["The final result includes value ok."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"agent_task": {"effort": {"execution": {"step_plan": "dag"}}}},
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "dynamic_task",
            "step_instruction": "Run the DAG-shaped extraction step.",
            "expected_evidence": "TaskDAG semantic output includes value ok.",
            "rationale": "The step has a clear bounded DAG contract.",
            "dynamic_task": {
                "mode": "submitted",
                "plan": graph,
                "handlers": {"local_handler": run_task},
                "graph_input": {"value": "ok"},
            },
        }

    cast(Any, task)._agent_task_step_overrides = {"_request_plan": request_plan}

    result = await task.async_run()
    meta = await task.async_meta()
    first_iteration = meta["iterations"][0]

    assert result["status"] == "completed"
    assert first_iteration["plan"]["execution_shape"] == "dynamic_task"
    assert first_iteration["plan"]["effective_execution_shape"] == "dynamic_task"
    assert first_iteration["plan"]["step_execution"]["dynamic_task_candidate_added"] is True
    assert first_iteration["execution_meta"]["route_plan"]["selected_route"] == "dynamic_task"
    assert first_iteration["execution_meta"]["logs"]["route_logs"]["task_dag"]["semantic_outputs"]["final"]["result"][
        "value"
    ] == "ok"
    assert getattr(agent, "_dynamic_task_candidates", []) == []


@pytest.mark.asyncio
async def test_agent_task_loop_actions_step_route_policy_prevents_skill_takeover(tmp_path):
    class ActionStepRequester(MockAgentTaskRequester):
        name = "ActionStepRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "the action-shaped step returned bounded evidence",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "source evidence collected",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "collected repository source evidence",
                    "evidence": ["fetch_agently_architecture_sources returned bounded excerpts"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-action-step-route-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-action-step-route-plugins")
    plugin_manager.register("ModelRequester", ActionStepRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-action-step-route")

    def fetch_agently_architecture_sources():
        return {"status": "ok", "sources": ["architecture evidence"]}

    skill_source = tmp_path / "skill-source" / "architecture-diagram"
    skill_source.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text(
        """---
name: architecture-diagram
description: Use for architecture diagram rendering.
---

# architecture-diagram

Render the final diagram only after source evidence is collected.
""",
        encoding="utf-8",
    )
    Agently.skills_executor.configure(
        registry_root=tmp_path / "skills-registry",
        allowed_trust_levels=["local"],
    )
    Agently.skills_executor.install_skills(skill_source, trust_level="local")

    agent.use_actions(fetch_agently_architecture_sources, always=True)
    agent.use_skills(["architecture-diagram"], mode="model_decision", always=True)

    task = agent.create_task(
        task_id="action-step-route-policy",
        goal="Gather source evidence before rendering with a skill.",
        success_criteria=["Source evidence is collected."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "actions",
            "step_instruction": "Call fetch_agently_architecture_sources before using any Skill.",
            "expected_evidence": "Repository source evidence is collected.",
            "rationale": "The task must gather source evidence before rendering.",
        }

    cast(Any, task)._agent_task_step_overrides = {"_request_plan": request_plan}

    result = await task.async_run()
    meta = await task.async_meta()
    first_iteration = meta["iterations"][0]

    assert result["status"] == "completed"
    assert first_iteration["plan"]["execution_shape"] == "actions"
    assert first_iteration["plan"]["step_execution"]["route_policy"]["allowed_routes"] == ["model_request"]
    assert first_iteration["execution_meta"]["route_plan"]["selected_route"] == "model_request"
    assert first_iteration["execution_meta"]["route_plan"]["candidates"]["skills"]["model_decision"] is True


@pytest.mark.asyncio
async def test_agent_task_loop_stops_at_max_iterations(tmp_path):
    class NeverCompleteRequester(MockAgentTaskRequester):
        name = "NeverCompleteRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload: dict[str, Any] = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "still incomplete",
                    "missing_criteria": ["final answer missing"],
                    "replan_instruction": "try one more step",
                    "final_result": "",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "continue analysis",
                    "expected_evidence": "final answer",
                    "rationale": "more evidence needed",
                }
            else:
                payload = {"step_result": "partial", "evidence": ["partial"], "remaining_work": ["final"]}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-max-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-max-plugins")
    plugin_manager.register("ModelRequester", NeverCompleteRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-max")

    task = agent.create_task(
        task_id="survey-analysis",
        goal="Analyze customer interview responses.",
        success_criteria=["pain points are identified"],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "max_iterations"
    assert result["accepted"] is False
    assert result["artifact_status"] == "partial"
    assert meta["status"] == "max_iterations"
    assert len(meta["iterations"]) == 1
    assert len(meta["workspace_refs"]["decisions"]) == 1
    assert len(meta["workspace_refs"]["verification"]) == 1


@pytest.mark.asyncio
async def test_agent_task_loop_blocks_when_verifier_requires_block(tmp_path):
    class BlockedRequester(MockAgentTaskRequester):
        name = "BlockedRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload: dict[str, Any] = {
                    "is_complete": True,
                    "requires_block": True,
                    "reason": "external approval is required before continuing",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "draft result should not be accepted",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "prepare the approval-bound change",
                    "expected_evidence": "approval state",
                    "rationale": "the task cannot safely continue without approval",
                }
            else:
                payload = {
                    "step_result": "approval is still pending",
                    "evidence": ["approval_required"],
                    "remaining_work": ["wait for approval"],
                }
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-blocked-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-blocked-plugins")
    plugin_manager.register("ModelRequester", BlockedRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-blocked")

    task = agent.create_task(
        task_id="blocked-approval",
        goal="Produce the final remediation report after external approval.",
        success_criteria=["The final report is returned only after approval is available."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "blocked"
    assert result["accepted"] is False
    assert result["artifact_status"] == "blocked"
    assert meta["status"] == "blocked"
    assert len(meta["iterations"]) == 1
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is False
    assert verification["requires_block"] is True
    assert "requires_block_true" in verification["guard_reasons"]
    assert meta["diagnostics"]["verification_guards"][0]["guard_reasons"] == ["requires_block_true"]
    assert meta["diagnostics"]["phases"][-1]["phase"] == "terminal"
    assert meta["diagnostics"]["phases"][-1]["diagnostics"]["artifact_status"] == "blocked"


@pytest.mark.asyncio
async def test_agent_task_loop_verification_guard_replans_when_missing_criteria_is_present(tmp_path):
    class CompleteWithMissingRequester(MockAgentTaskRequester):
        name = "CompleteWithMissingRequester"
        verification_calls = 0

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                CompleteWithMissingRequester.verification_calls += 1
                if CompleteWithMissingRequester.verification_calls == 1:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "looks complete but readback is missing",
                        "missing_criteria": ["file readback missing"],
                        "replan_instruction": "",
                        "final_result": "done",
                    }
                else:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "readback evidence is now present",
                        "missing_criteria": [],
                        "replan_instruction": "",
                        "final_result": "legacy script upgraded and verified",
                    }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "repair the legacy script using current Agently APIs",
                    "expected_evidence": "script execution succeeds and file is read back",
                    "rationale": "the prior verification gap must be closed",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "patched script and ran verification",
                    "evidence": ["python legacy_script.py exited with status 0", "file readback succeeded"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-guard-missing-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-guard-missing-plugins")
    plugin_manager.register("ModelRequester", CompleteWithMissingRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-guard-missing")

    task = agent.create_task(
        task_id="guard-missing",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The final file readback evidence is included."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    stream_items = [item async for item in task.stream()]
    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert result["iterations"] == 2
    assert any(item.path.endswith(".replan") for item in stream_items)
    assert meta["iterations"][0]["verification"]["is_complete"] is False
    assert "missing_criteria_present" in meta["iterations"][0]["verification"]["guard_reasons"]
    assert meta["diagnostics"]["verification_guards"]


@pytest.mark.asyncio
async def test_agent_task_loop_verification_guard_replans_when_final_result_missing(tmp_path):
    class CompleteWithoutFinalResultRequester(MockAgentTaskRequester):
        name = "CompleteWithoutFinalResultRequester"
        verification_calls = 0

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                CompleteWithoutFinalResultRequester.verification_calls += 1
                if CompleteWithoutFinalResultRequester.verification_calls == 1:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "all evidence is present but no final deliverable was returned",
                        "missing_criteria": [],
                        "replan_instruction": "",
                        "final_result_required": True,
                        "final_result": "",
                    }
                else:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "final deliverable is now included",
                        "missing_criteria": [],
                        "replan_instruction": "",
                        "final_result_required": True,
                        "final_result": "Final remediation report with verification evidence.",
                    }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "produce the final report artifact",
                    "expected_evidence": "final report text",
                    "rationale": "the verifier requires the final deliverable before acceptance",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "prepared final report artifact",
                    "evidence": ["final report content is present"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-final-result-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-final-result-plugins")
    plugin_manager.register("ModelRequester", CompleteWithoutFinalResultRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-final-result")

    task = agent.create_task(
        task_id="guard-final-result",
        goal="Return the final remediation report.",
        success_criteria=["The final report artifact is returned."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    stream_items = [item async for item in task.stream()]
    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["artifact_status"] == "accepted"
    assert result["iterations"] == 2
    assert any(item.path.endswith(".replan") for item in stream_items)
    first_verification = meta["iterations"][0]["verification"]
    assert first_verification["is_complete"] is False
    assert "final_result_missing" in first_verification["guard_reasons"]
    assert "Final result is missing." in first_verification["missing_criteria"]
    assert first_verification["replan_instruction"]
    assert meta["iterations"][1]["verification"]["final_result"] == "Final remediation report with verification evidence."


@pytest.mark.asyncio
async def test_agent_task_loop_verification_guard_replans_on_failed_action_evidence(tmp_path):
    class AlwaysCompleteRequester(MockAgentTaskRequester):
        name = "AlwaysCompleteRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "all criteria are satisfied",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "legacy script upgraded and verified",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "run the verification command",
                    "expected_evidence": "command succeeds",
                    "rationale": "the task needs command evidence",
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-guard-action-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-guard-action-plugins")
    plugin_manager.register("ModelRequester", AlwaysCompleteRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-guard-action")
    task = agent.create_task(
        task_id="guard-action",
        goal="Repair a legacy Agently script and return the final verified result.",
        success_criteria=["The verification command succeeds.", "The final result is returned."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    async def fake_execute(iteration_index, plan, context_pack):
        _ = (plan, context_pack)
        status = "failed" if iteration_index == 1 else "success"
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": [status], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {
                    "action_logs": {
                        "run_task_command": {
                            "name": "run_task_command",
                            "status": status,
                            "action_type": "shell",
                        }
                    }
                },
            },
        )

    cast(Any, task)._agent_task_step_overrides = {"_execute_step": fake_execute}

    stream_items = [item async for item in task.stream()]
    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert result["iterations"] == 2
    assert any(item.path.endswith(".replan") for item in stream_items)
    first_verification = meta["iterations"][0]["verification"]
    assert first_verification["is_complete"] is False
    assert "execution_risk_actions_present" in first_verification["guard_reasons"]
    second_logs = meta["iterations"][1]["execution_meta"]["logs"]["action_logs"]
    assert second_logs["run_task_command"]["status"] == "success"


@pytest.mark.asyncio
async def test_required_capabilities_satisfied_cumulatively_across_iterations(tmp_path):
    """ISSUE-012: required actions and skills can be satisfied in different steps."""
    class CumulativeRequester(MockAgentTaskRequester):
        name = "CumulativeRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "evidence is present",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": False,
                    "final_result": "done",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {"step_instruction": "produce capability evidence", "expected_evidence": "x", "rationale": "y"}
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-cumulative-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-cumulative-plugins")
    plugin_manager.register("ModelRequester", CumulativeRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-cumulative")

    constraints = {"capability_constraints": {"actions": {"required": ["act_x"]}, "skills": {"required": ["skill_y"]}}}

    async def step_with_capability(iteration_index, plan, context_pack):
        # iteration 1 satisfies the required action, iteration 2 the required skill.
        if iteration_index == 1:
            logs = {"action_logs": {"act_x": {"name": "act_x", "status": "success"}}, "route_logs": {}}
        else:
            logs = {"action_logs": {}, "route_logs": {"plan": {"selected_skills": [{"skill_id": "skill_y"}]}}}
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": ["ok"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request" if iteration_index == 1 else "skills"},
                "logs": logs,
                "effective_options": constraints,
            },
        )

    task = agent.create_task(
        task_id="cumulative-capabilities",
        goal="Satisfy required action and skill across steps.",
        success_criteria=["The required action and skill are both used."],
        workspace=tmp_path / "task-workspace",
        max_iterations=3,
        options={"capability_constraints": constraints["capability_constraints"]},
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": step_with_capability}

    result = await task.async_run()
    meta = await task.async_meta()

    # Iteration 1 cannot complete (skill_y still missing); iteration 2 completes.
    assert result["status"] == "completed"
    assert result["iterations"] == 2
    first = meta["iterations"][0]["verification"]
    assert first["is_complete"] is False
    assert "skill_y" in " ".join(first.get("missing_required_capabilities", []))


@pytest.mark.asyncio
async def test_task_wall_clock_budget_surfaces_timed_out(tmp_path):
    """ISSUE-010: max_seconds is a task wall-clock deadline across task stages."""
    agent = _create_agent("agent-task-deadline").use_workspace(tmp_path / "task-workspace")
    task = agent.create_task(
        task_id="deadline",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=3,
        limits={"max_seconds": 0.2},
    )

    async def slow_request_plan(_iteration_index, _context_pack):
        await asyncio.sleep(0.6)
        return {
            "step_instruction": "repair the script",
            "expected_evidence": "script execution succeeds",
            "rationale": "the task should not reach this plan after the deadline",
        }

    cast(Any, task)._agent_task_step_overrides = {"_request_plan": slow_request_plan}

    result = await task.async_run()
    assert result["status"] == "timed_out"
    assert task.status == "timed_out"
    assert "plan stage" in result["reason"]


def test_action_final_status_exempts_recovered_actions():
    """ISSUE-012: an action that failed then succeeded is not a risk action."""
    from agently.core.application.AgentTask.Task import AgentTask

    statuses = {"act_a": "success", "act_b": "failed"}
    failed = AgentTask._action_ids_by_final_status(statuses, {"failed", "failure", "error"})
    assert failed == ["act_b"]
    assert "act_a" not in failed


@pytest.mark.asyncio
async def test_interview_semantic_judge_returns_structured_rule_fields():
    class SemanticJudgeRequester(MockAgentTaskRequester):
        name = "SemanticJudgeRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            assert "Judge the candidate interview brief semantically" in text
            payload = {
                "accepted": False,
                "source_specificity_ok": False,
                "target_coverage_ok": True,
                "conflict_handling_ok": False,
                "low_evidence_handling_ok": False,
                "blog_interview_quality_ok": True,
                "not_hiring_framed_ok": True,
                "reason": "Sources are too generic and uncertainty is not handled.",
                "rule_evidence": [
                    {
                        "rule": "source_specificity",
                        "ok": False,
                        "evidence": "The brief says sources exist but gives no URL or title.",
                    }
                ],
            }
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="interview-semantic-judge-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="interview-semantic-judge-plugins")
    plugin_manager.register("ModelRequester", SemanticJudgeRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="interview-semantic-judge")

    result = await judge_interview_semantics(
        agent,
        file_text="# Interview brief\n\nSources: public web.\n\nQuestions?\n",
        interview_input={
            "targets": [
                {
                    "raw_input": "Karpathy from Anthropic",
                    "original_name": "Karpathy",
                    "organization_or_work": "Anthropic",
                    "aliases": [],
                }
            ],
            "interview_goal": "Prepare a blog interview brief.",
        },
        success_criteria=["The brief handles source evidence and target ambiguity."],
        action_summary={"action_log_count": 1, "action_log_ids": ["web_search"]},
    )

    assert result["accepted"] is False
    assert result["source_specificity_ok"] is False
    assert result["target_coverage_ok"] is True
    assert result["rule_evidence"]


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_failure_is_side_channel(tmp_path):
    MockAgentTaskRequester.reset()

    class FailingProgressRequester(MockAgentTaskRequester):
        name = "FailingProgressRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Summarize AgentTask progress" in text:
                raise RuntimeError("progress model unavailable")
            async for event in super().request_model(request_data):
                yield event

    settings = Settings(name="agent-task-failing-progress-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-failing-progress-plugins")
    plugin_manager.register("ModelRequester", FailingProgressRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-failing-progress")
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_agent_task_loop_progress_model_failure_is_side_channel.capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        task = agent.create_task(
            task_id="failing-progress",
            goal="Repair a legacy Agently script so it runs on the current API.",
            success_criteria=["The script runs successfully."],
            workspace=tmp_path / "task-workspace",
            max_iterations=1,
            options={
                "agent_task": {
                    "stream_progress": True,
                    "progress_model_key": "progress-narrator",
                    "progress_timeout_seconds": 5,
                },
            },
        )

        result = await task.async_run()
        meta = await task.async_meta()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    event_types = [event.event_type for event in captured]
    side_channel_events = [
        event
        for event in captured
        if event.event_type in {"model.side_channel_request_failed", "request.side_channel_failed"}
    ]

    assert result["status"] == "max_iterations"
    assert side_channel_events
    assert "model.request_failed" not in event_types
    assert "request.failed" not in event_types
    assert all(event.level == "WARNING" for event in side_channel_events)
    assert meta["diagnostics"]["progress_errors"]


async def _collect_stream(task) -> list[Any]:
    return [item async for item in task.stream()]
