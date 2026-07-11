from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest


def test_core_root_exports_remain_stable():
    from agently.core import (
        Action,
        AgentExecutionResult,
        AgentExecutionStream,
        BaseAgent,
        DynamicTask,
        EventCenter,
        ExecutionResourceManager,
        ExtensionHandlers,
        ModelRequest,
        ModelRequestResult,
        ModelResponse,
        PluginManager,
        Prompt,
        RuntimeEvent,
        Session,
        SkillsManager,
        SkillsExecutor,
        TaskBoard,
        TaskBoardValidator,
        Tool,
        TriggerFlow,
        Workspace,
        WorkspaceManager,
    )

    assert BaseAgent.__name__ == "BaseAgent"
    assert AgentExecutionResult.__name__ == "AgentExecutionResult"
    assert AgentExecutionStream.__name__ == "AgentExecutionStream"
    assert DynamicTask.__name__ == "DynamicTask"
    assert EventCenter.__name__ == "EventCenter"
    assert ExecutionResourceManager.__name__ == "ExecutionResourceManager"
    assert ExtensionHandlers.__name__ == "ExtensionHandlers"
    assert ModelRequest.__name__ == "ModelRequest"
    assert ModelRequestResult.__name__ == "ModelRequestResult"
    assert ModelResponse.__name__ == "ModelResponse"
    import agently.core as agently_core

    assert not hasattr(agently_core, "ModelResponseResult")
    assert PluginManager.__name__ == "PluginManager"
    assert Prompt.__name__ == "Prompt"
    assert RuntimeEvent.__name__ == "RuntimeEvent"
    assert Session.__name__ == "Session"
    assert SkillsManager.__name__ == "SkillsManager"
    assert SkillsExecutor.__name__ == "SkillsExecutor"
    assert TaskBoard.__name__ == "TaskBoard"
    assert TaskBoardValidator.__name__ == "TaskBoardValidator"
    assert Action.__name__ == "Action"
    assert Tool is Action
    assert TriggerFlow.__name__ == "TriggerFlow"
    assert Workspace.__name__ == "Workspace"
    assert WorkspaceManager.__name__ == "WorkspaceManager"


def test_execution_exchange_types_are_publicly_importable():
    from agently.types.data import (
        ExecutionExchangeKind,
        ExecutionExchangeProviderResult,
        ExecutionExchangeRequest,
    )
    from agently.types.plugins import ExecutionExchangeProvider
    from agently.types.trigger_flow import TriggerFlowExternalWaitRequest

    exchange_kinds = set(get_args(ExecutionExchangeKind))
    assert "guidance" in exchange_kinds
    assert "supplement" not in exchange_kinds
    assert ExecutionExchangeProviderResult.__name__ == "ExecutionExchangeProviderResult"
    assert ExecutionExchangeRequest.__name__ == "ExecutionExchangeRequest"
    assert TriggerFlowExternalWaitRequest is ExecutionExchangeRequest
    assert ExecutionExchangeProvider.__name__ == "ExecutionExchangeProvider"


def test_core_topic_packages_expose_canonical_import_paths():
    from agently.core.application.AgentExecution import AgentExecutionStream
    from agently.core.application.SkillsManager import SkillsManager
    from agently.core.application.SkillsExecutor import SkillsExecutor
    from agently.core.Agent import BaseAgent
    from agently.core.operation.Action import Action, Tool
    from agently.core.operation.ExecutionResource import ExecutionResourceManager
    from agently.core.extension import ExtensionHandlers, PluginManager
    from agently.core.model import ModelRequest, ModelRequestResult, ModelResponse, Prompt
    from agently.core.application.DynamicTask import DynamicTask
    from agently.core.orchestration.TaskBoard import TaskBoard
    from agently.core.orchestration.TaskDAG import TaskDAGExecutor
    from agently.core.orchestration.TriggerFlow import TriggerFlow
    from agently.core.model import AttemptRunner
    from agently.core.runtime import EventCenter, RuntimeEvent, bind_runtime_context
    from agently.core.session import Session
    from agently.core.Workspace import ContextProfile, Workspace

    assert importlib.import_module("agently.core.Agent").BaseAgent is BaseAgent
    assert importlib.import_module("agently.core.application.AgentExecution.Stream").AgentExecutionStream is AgentExecutionStream
    assert importlib.import_module("agently.core.model.ModelRequest").ModelRequest is ModelRequest
    assert importlib.import_module("agently.core.model.ModelRequestResult").ModelRequestResult is ModelRequestResult
    assert importlib.import_module("agently.core.model.ModelResponse").ModelResponse is ModelResponse
    assert importlib.util.find_spec("agently.core.model.ModelResponseResult") is None
    assert importlib.import_module("agently.core.model.Prompt").Prompt is Prompt
    assert importlib.import_module("agently.core.model.AttemptRunner").AttemptRunner is AttemptRunner
    assert importlib.import_module("agently.core.runtime.EventCenter").EventCenter is EventCenter
    assert importlib.import_module("agently.core.runtime").RuntimeEvent is RuntimeEvent
    assert importlib.import_module("agently.core.runtime.RuntimeContext").bind_runtime_context is bind_runtime_context
    assert importlib.import_module("agently.core.operation.ExecutionResource.ExecutionResource").ExecutionResourceManager is ExecutionResourceManager
    assert importlib.import_module("agently.core.extension.PluginManager").PluginManager is PluginManager
    assert importlib.import_module("agently.core.extension.ExtensionHandlers").ExtensionHandlers is ExtensionHandlers
    assert importlib.import_module("agently.core.session.Session").Session is Session
    assert importlib.import_module("agently.core.application.DynamicTask.DynamicTask").DynamicTask is DynamicTask
    assert importlib.import_module("agently.core.orchestration.TaskBoard.TaskBoardRuntime").TaskBoard is TaskBoard
    assert importlib.import_module("agently.core.orchestration.TaskDAG.TaskDAGExecutor").TaskDAGExecutor is TaskDAGExecutor
    assert importlib.import_module("agently.core.orchestration.TriggerFlow.TriggerFlow").TriggerFlow is TriggerFlow
    assert importlib.import_module("agently.core.operation.Action.Action").Action is Action
    assert importlib.import_module("agently.core.operation.Action").Tool is Tool
    assert importlib.import_module("agently.core.application.SkillsManager.SkillsManager").SkillsManager is SkillsManager
    assert importlib.import_module("agently.core.application.SkillsExecutor.SkillsExecutor").SkillsExecutor is SkillsExecutor
    assert importlib.import_module("agently.core.Workspace.Workspace").Workspace is Workspace
    assert importlib.import_module("agently.core.Workspace.ContextBuilder").ContextProfile is ContextProfile


def test_workspace_package_uses_class_owned_canonical_name_with_released_alias():
    from agently.core import Workspace as RootWorkspace
    from agently.core.Workspace import Workspace as CanonicalWorkspace

    canonical_package = importlib.import_module("agently.core.Workspace")
    assert canonical_package.__file__ is not None
    canonical_root = Path(canonical_package.__file__).resolve().parent
    core_root = canonical_root.parent
    workspace_entries = sorted(path.name for path in core_root.iterdir() if path.name.casefold().startswith("workspace"))
    workspace_directories = sorted(
        path.name for path in core_root.iterdir() if path.is_dir() and path.name.casefold() == "workspace"
    )

    assert canonical_root.name == "Workspace"
    assert workspace_entries == ["Workspace", "workspace.py", "workspace.pyi"]
    assert (core_root / "Workspace").is_dir()
    assert (core_root / "workspace.py").is_file()
    assert (core_root / "workspace.pyi").is_file()
    assert workspace_directories == ["Workspace"]
    assert CanonicalWorkspace.__module__ == "agently.core.Workspace.Workspace"
    assert canonical_package.Workspace is CanonicalWorkspace
    assert RootWorkspace is CanonicalWorkspace

    from agently.core.workspace import LocalVectorIndex as ReleasedLocalVectorIndex

    assert ReleasedLocalVectorIndex is canonical_package.LocalVectorIndex


def test_workspace_lowercase_compatibility_is_a_non_package_module():
    from agently.core.Workspace import Workspace as CanonicalWorkspace

    canonical_package = importlib.import_module("agently.core.Workspace")
    compatibility_module = importlib.import_module("agently.core.workspace")

    assert compatibility_module.__spec__ is not None
    assert compatibility_module.__spec__.name == "agently.core.workspace"
    assert compatibility_module.__spec__.origin is not None
    assert Path(compatibility_module.__spec__.origin).name == "workspace.py"
    assert not hasattr(compatibility_module, "__path__")

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agently.core.workspace.Workspace")

    assert "agently.core.workspace.Workspace" not in sys.modules
    assert canonical_package.Workspace is CanonicalWorkspace


@pytest.mark.parametrize(
    "imports",
    [
        """
from agently.core.Workspace import Workspace as CanonicalWorkspace
from agently.core import Workspace as RootWorkspace
from agently.core.workspace import LocalVectorIndex as ReleasedLocalVectorIndex
""",
        """
from agently.core.workspace import LocalVectorIndex as ReleasedLocalVectorIndex
from agently.core import Workspace as RootWorkspace
from agently.core.Workspace import Workspace as CanonicalWorkspace
""",
    ],
)
def test_workspace_import_orders_preserve_canonical_and_root_symbols(imports: str):
    repository_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            imports
            + """
import importlib
import agently.core as core

canonical_package = importlib.import_module("agently.core.Workspace")
compatibility_module = importlib.import_module("agently.core.workspace")
assert RootWorkspace is CanonicalWorkspace
assert core.Workspace is CanonicalWorkspace
assert canonical_package.Workspace is CanonicalWorkspace
assert ReleasedLocalVectorIndex is canonical_package.LocalVectorIndex
assert compatibility_module is not canonical_package
assert not hasattr(compatibility_module, "__path__")
""",
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_core_layout_keeps_only_classified_root_packages():
    core_root = Path(__file__).resolve().parents[2] / "agently" / "core"

    root_files = sorted(path.name for path in core_root.iterdir() if path.is_file())
    root_dirs = sorted(path.name for path in core_root.iterdir() if path.is_dir() and path.name != "__pycache__")

    assert root_files == ["Agent.py", "__init__.py", "workspace.py", "workspace.pyi"]
    assert root_dirs == [
        "Workspace",
        "application",
        "extension",
        "model",
        "operation",
        "orchestration",
        "runtime",
        "session",
    ]
    assert (core_root / "application" / "AgentExecution").is_dir()
    assert (core_root / "application" / "SkillsManager").is_dir()
    assert (core_root / "application" / "SkillsExecutor").is_dir()
    assert (core_root / "operation" / "Action").is_dir()
    assert (core_root / "operation" / "ExecutionResource").is_dir()
    assert (core_root / "Workspace").is_dir()
    assert (core_root / "Workspace" / "ContextBuilder").is_dir()
    assert not (core_root / "Workspace" / "Recall").exists()
    assert not (core_root / "session" / "Workspace").exists()
    assert not (core_root / "session" / "Recall").exists()
    assert (core_root / "orchestration" / "TriggerFlow").is_dir()
    assert (core_root / "orchestration" / "TaskDAG").is_dir()
    assert (core_root / "orchestration" / "TaskBoard").is_dir()
    assert (core_root / "application" / "DynamicTask").is_dir()
    assert not (core_root / "orchestration" / "TaskDAGExecutor").exists()
    assert not (core_root / "orchestration" / "DynamicTask").exists()

    assert not (core_root / "Tool.py").exists()
    assert not (core_root / "Tool").exists()
    assert not (core_root / "foundation").exists()
    assert not (core_root / "execution").exists()
    assert not (core_root / "operation" / "Tool.py").exists()
    assert not (core_root / "operation" / "Tool").exists()


def test_removed_flat_core_submodules_do_not_resolve():
    removed_modules = [
        "agently.core.ModelRequest",
        "agently.core.RuntimeEvents",
        "agently.core.Tool",
        "agently.core.orchestration.DynamicTask",
        "agently.core.orchestration.TaskDAGExecutor",
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
