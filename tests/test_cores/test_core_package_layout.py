from __future__ import annotations

import importlib
from pathlib import Path


def test_core_root_exports_remain_stable():
    from agently.core import (
        Action,
        AgentExecutionStream,
        BaseAgent,
        DynamicTask,
        EventCenter,
        ExecutionEnvironmentManager,
        ExtensionHandlers,
        ModelRequest,
        ModelResponse,
        ModelResponseResult,
        PluginManager,
        Prompt,
        RuntimeEvent,
        Session,
        SkillsExecutor,
        Tool,
        TriggerFlow,
        Workspace,
        WorkspaceManager,
    )

    assert BaseAgent.__name__ == "BaseAgent"
    assert AgentExecutionStream.__name__ == "AgentExecutionStream"
    assert DynamicTask.__name__ == "DynamicTask"
    assert EventCenter.__name__ == "EventCenter"
    assert ExecutionEnvironmentManager.__name__ == "ExecutionEnvironmentManager"
    assert ExtensionHandlers.__name__ == "ExtensionHandlers"
    assert ModelRequest.__name__ == "ModelRequest"
    assert ModelResponse.__name__ == "ModelResponse"
    assert ModelResponseResult.__name__ == "ModelResponseResult"
    assert PluginManager.__name__ == "PluginManager"
    assert Prompt.__name__ == "Prompt"
    assert RuntimeEvent.__name__ == "RuntimeEvent"
    assert Session.__name__ == "Session"
    assert SkillsExecutor.__name__ == "SkillsExecutor"
    assert Action.__name__ == "Action"
    assert Tool is Action
    assert TriggerFlow.__name__ == "TriggerFlow"
    assert Workspace.__name__ == "Workspace"
    assert WorkspaceManager.__name__ == "WorkspaceManager"


def test_core_topic_packages_expose_canonical_import_paths():
    from agently.core.application.AgentExecution import AgentExecutionStream
    from agently.core.application.SkillsExecutor import SkillsExecutor
    from agently.core.Agent import BaseAgent
    from agently.core.execution.Action import Action, Tool
    from agently.core.execution.ExecutionEnvironment import ExecutionEnvironmentManager
    from agently.core.extension import ExtensionHandlers, PluginManager
    from agently.core.model import ModelRequest, ModelResponse, ModelResponseResult, Prompt
    from agently.core.orchestration.DynamicTask import DynamicTask
    from agently.core.orchestration.TaskDAGExecutor import TaskDAGExecutor
    from agently.core.orchestration.TriggerFlow import TriggerFlow
    from agently.core.runtime import AttemptRunner, EventCenter, RuntimeEvent, bind_runtime_context
    from agently.core.session import RecallProfile, Session, Workspace

    assert importlib.import_module("agently.core.Agent").BaseAgent is BaseAgent
    assert importlib.import_module("agently.core.application.AgentExecution.Stream").AgentExecutionStream is AgentExecutionStream
    assert importlib.import_module("agently.core.model.ModelRequest").ModelRequest is ModelRequest
    assert importlib.import_module("agently.core.model.ModelResponse").ModelResponse is ModelResponse
    assert importlib.import_module("agently.core.model.ModelResponseResult").ModelResponseResult is ModelResponseResult
    assert importlib.import_module("agently.core.model.Prompt").Prompt is Prompt
    assert importlib.import_module("agently.core.runtime.AttemptRunner").AttemptRunner is AttemptRunner
    assert importlib.import_module("agently.core.runtime.EventCenter").EventCenter is EventCenter
    assert importlib.import_module("agently.core.runtime").RuntimeEvent is RuntimeEvent
    assert importlib.import_module("agently.core.runtime.RuntimeContext").bind_runtime_context is bind_runtime_context
    assert importlib.import_module("agently.core.execution.ExecutionEnvironment.ExecutionEnvironment").ExecutionEnvironmentManager is ExecutionEnvironmentManager
    assert importlib.import_module("agently.core.extension.PluginManager").PluginManager is PluginManager
    assert importlib.import_module("agently.core.extension.ExtensionHandlers").ExtensionHandlers is ExtensionHandlers
    assert importlib.import_module("agently.core.session.Session").Session is Session
    assert importlib.import_module("agently.core.orchestration.DynamicTask.DynamicTask").DynamicTask is DynamicTask
    assert importlib.import_module("agently.core.orchestration.TaskDAGExecutor.TaskDAGExecutor").TaskDAGExecutor is TaskDAGExecutor
    assert importlib.import_module("agently.core.orchestration.TriggerFlow.TriggerFlow").TriggerFlow is TriggerFlow
    assert importlib.import_module("agently.core.execution.Action.Action").Action is Action
    assert importlib.import_module("agently.core.execution.Action").Tool is Tool
    assert importlib.import_module("agently.core.application.SkillsExecutor.SkillsExecutor").SkillsExecutor is SkillsExecutor
    assert importlib.import_module("agently.core.session.Workspace.Workspace").Workspace is Workspace
    assert importlib.import_module("agently.core.session.Recall").RecallProfile is RecallProfile


def test_core_layout_keeps_only_classified_root_packages():
    core_root = Path(__file__).resolve().parents[2] / "agently" / "core"

    root_files = sorted(path.name for path in core_root.iterdir() if path.is_file())
    root_dirs = sorted(path.name for path in core_root.iterdir() if path.is_dir() and path.name != "__pycache__")

    assert root_files == ["Agent.py", "__init__.py"]
    assert root_dirs == [
        "application",
        "execution",
        "extension",
        "model",
        "orchestration",
        "runtime",
        "session",
    ]
    assert (core_root / "application" / "AgentExecution").is_dir()
    assert (core_root / "application" / "SkillsExecutor").is_dir()
    assert (core_root / "execution" / "Action").is_dir()
    assert (core_root / "execution" / "ExecutionEnvironment").is_dir()
    assert (core_root / "session" / "Workspace").is_dir()
    assert (core_root / "session" / "Recall").is_dir()
    assert (core_root / "orchestration" / "TriggerFlow").is_dir()
    assert (core_root / "orchestration" / "TaskDAGExecutor").is_dir()
    assert (core_root / "orchestration" / "DynamicTask").is_dir()

    assert not (core_root / "Tool.py").exists()
    assert not (core_root / "Tool").exists()
    assert not (core_root / "foundation").exists()
    assert not (core_root / "execution" / "Tool.py").exists()
    assert not (core_root / "execution" / "Tool").exists()


def test_removed_flat_core_submodules_do_not_resolve():
    removed_modules = [
        "agently.core.ModelRequest",
        "agently.core.RuntimeEvents",
        "agently.core.Tool",
    ]

    for module_name in removed_modules:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        raise AssertionError(f"{module_name} should use the root export or canonical topic package")


def test_runtime_package_has_no_provider_specific_boundary_leakage():
    runtime_root = Path(__file__).resolve().parents[2] / "agently" / "core" / "runtime"
    forbidden_terms = [
        "OpenAI",
        "Anthropic",
        "api_key",
        "headers",
        "SSE",
        "HTTP",
    ]

    for source_file in runtime_root.rglob("*.py"):
        source = source_file.read_text(encoding="utf-8")
        assert [term for term in forbidden_terms if term in source] == []
